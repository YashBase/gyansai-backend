"""iter14: Share-link claim flow tests.
Covers:
- POST /api/exams/{exam_id}/claim (student only, idempotent, 403 if not published, 404 if not found)
- POST /api/auth/signup with from_share_link=true → auto-approved + exam grant
- Regular signup still returns auto_approved=false
"""
import os
import random
import pytest
import requests

def _read_frontend_env():
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or _read_frontend_env() or "").rstrip("/")
assert BASE_URL, "REACT_APP_BACKEND_URL must be set"
API = BASE_URL + "/api"

EXAM_ID = "562bc01a-b273-4f2b-a362-de6c63f760d1"  # 'jee' exam, confirmed published

ADMIN_EMAIL = "admin@gyansai.com"
ADMIN_PASS = "admin123"
DEMO_USER = "demo"
DEMO_PASS = "demo123"


def _rand_mobile():
    # iter14 uses 91230xxxxx prefix (per main agent note)
    return "91230" + str(random.randint(10000, 99999))


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


@pytest.fixture(scope="module")
def admin_token(s):
    r = s.post(f"{API}/auth/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text}")
    return r.json()["token"]


@pytest.fixture(scope="module")
def demo_token(s):
    r = s.post(f"{API}/auth/student/login", json={"username": DEMO_USER, "password": DEMO_PASS})
    if r.status_code != 200:
        pytest.skip(f"demo login failed: {r.status_code} {r.text}")
    return r.json()["token"]


# ---------- POST /exams/{exam_id}/claim ----------

class TestClaimEndpoint:
    def test_claim_requires_auth(self, s):
        r = s.post(f"{API}/exams/{EXAM_ID}/claim")
        assert r.status_code in (401, 403)

    def test_claim_rejects_admin(self, s, admin_token):
        r = s.post(f"{API}/exams/{EXAM_ID}/claim", headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 403
        assert "student" in r.json().get("detail", "").lower()

    def test_claim_returns_404_for_missing_exam(self, s, demo_token):
        r = s.post(f"{API}/exams/nonexistent-exam-id/claim",
                   headers={"Authorization": f"Bearer {demo_token}"})
        assert r.status_code == 404

    def test_claim_success_and_idempotent(self, s, demo_token):
        h = {"Authorization": f"Bearer {demo_token}"}
        r1 = s.post(f"{API}/exams/{EXAM_ID}/claim", headers=h)
        assert r1.status_code == 200, r1.text
        d1 = r1.json()
        assert d1.get("ok") is True
        assert d1.get("exam_id") == EXAM_ID

        # Idempotent
        r2 = s.post(f"{API}/exams/{EXAM_ID}/claim", headers=h)
        assert r2.status_code == 200

        # Verify persisted on the student record
        me = s.get(f"{API}/auth/me", headers=h)
        assert me.status_code == 200
        assert EXAM_ID in me.json().get("exam_ids", [])

    def test_claim_403_when_unpublished(self, s, admin_token, demo_token):
        # Create a new unpublished exam and try to claim
        h_admin = {"Authorization": f"Bearer {admin_token}"}
        h_stud = {"Authorization": f"Bearer {demo_token}"}
        payload = {
            "name": "TEST_iter14_unpublished",
            "exam_type": "custom",
            "class_level": "12th",
            "duration_minutes": 30,
            "question_ids": [],
            "is_published": False,
        }
        cr = s.post(f"{API}/exams", json=payload, headers=h_admin)
        assert cr.status_code in (200, 201), cr.text
        new_id = cr.json().get("id") or cr.json().get("exam_id")
        assert new_id

        try:
            r = s.post(f"{API}/exams/{new_id}/claim", headers=h_stud)
            assert r.status_code == 403
            assert "not" in r.json().get("detail", "").lower()
        finally:
            s.delete(f"{API}/exams/{new_id}", headers=h_admin)


# ---------- POST /auth/signup with from_share_link ----------

class TestSignupShareLink:
    created_student_ids = []

    def test_share_link_signup_auto_approves_and_grants_exam(self, s, admin_token):
        mobile = _rand_mobile()
        payload = {
            "name": "TEST_iter14_share",
            "mobile": mobile,
            "password": "pass1234",
            "class_level": "12th",
            "from_share_link": True,
            "target_exam_id": EXAM_ID,
        }
        r = s.post(f"{API}/auth/signup", json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("auto_approved") is True
        assert "token" in data
        assert data.get("role") == "student"
        user = data.get("user", {})
        assert user.get("signup_status") == "approved"
        assert EXAM_ID in user.get("exam_ids", [])
        sid = user.get("id")
        assert sid
        TestSignupShareLink.created_student_ids.append(sid)

        # Token should be usable
        me = s.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {data['token']}"})
        assert me.status_code == 200
        assert EXAM_ID in me.json().get("exam_ids", [])

        # And exam.assigned_student_ids should contain the new student id
        h_admin = {"Authorization": f"Bearer {admin_token}"}
        er = s.get(f"{API}/exams/{EXAM_ID}", headers=h_admin)
        assert er.status_code == 200
        assert sid in er.json().get("assigned_student_ids", [])

    def test_regular_signup_pending(self, s):
        mobile = _rand_mobile()
        r = s.post(f"{API}/auth/signup", json={
            "name": "TEST_iter14_regular",
            "mobile": mobile,
            "password": "pass1234",
            "class_level": "11th",
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("auto_approved") is False
        assert "token" not in d
        assert "approval" in (d.get("message") or "").lower()

    def test_share_link_signup_duplicate_mobile_409(self, s):
        mobile = _rand_mobile()
        payload = {
            "name": "TEST_iter14_dup",
            "mobile": mobile,
            "password": "pass1234",
            "class_level": "12th",
            "from_share_link": True,
            "target_exam_id": EXAM_ID,
        }
        r1 = s.post(f"{API}/auth/signup", json=payload)
        assert r1.status_code == 200
        TestSignupShareLink.created_student_ids.append(r1.json()["user"]["id"])

        r2 = s.post(f"{API}/auth/signup", json=payload)
        assert r2.status_code == 409

    @classmethod
    def teardown_class(cls):
        # Best-effort cleanup
        try:
            sess = requests.Session()
            r = sess.post(f"{API}/auth/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
            if r.status_code == 200:
                tok = r.json()["token"]
                for sid in cls.created_student_ids:
                    sess.delete(f"{API}/admin/students/{sid}",
                                headers={"Authorization": f"Bearer {tok}"})
        except Exception:
            pass
