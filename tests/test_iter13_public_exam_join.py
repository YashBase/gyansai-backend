"""Iter12.5/iter13 — Public exam share-link join (guest quick-join) tests."""
import os
import time
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://iit-test-portal.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@gyansai.com"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"admin login failed {r.status_code} {r.text[:200]}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def published_exam(admin_headers):
    r = requests.get(f"{API}/exams", headers=admin_headers, timeout=15)
    assert r.status_code == 200
    exams = r.json()
    pub = [e for e in exams if e.get("is_published")]
    assert pub, "Need a published exam to test"
    # Prefer 'jee'
    for e in pub:
        if "jee" in (e.get("name") or "").lower():
            return e
    return pub[0]


# ---------- Public exam preview ----------
def test_public_exam_preview_unpublished_returns_404():
    fake = "00000000-not-a-real-exam"
    r = requests.get(f"{API}/public/exam/{fake}", timeout=10)
    assert r.status_code == 404


def test_public_exam_preview_published_no_auth(published_exam):
    eid = published_exam["id"]
    r = requests.get(f"{API}/public/exam/{eid}", timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == eid
    assert "name" in data
    assert "duration_minutes" in data
    assert "question_count" in data
    # Question ids and assigned ids must not leak
    assert "question_ids" not in data
    assert "assigned_student_ids" not in data


# ---------- Share link returns relative=/exam/{id} ----------
def test_share_link_uses_new_public_path(published_exam, admin_headers):
    eid = published_exam["id"]
    r = requests.post(f"{API}/exams/{eid}/share", headers=admin_headers, timeout=10)
    assert r.status_code == 200
    s = r.json()
    assert s["relative"] == f"/exam/{eid}"
    assert "/login?join" not in s["url"]
    assert f"/exam/{eid}" in s["url"]
    assert "/exam/" in s["whatsapp"]
    assert "/exam/" in s["email"]


# ---------- Guest Quick Join ----------
@pytest.fixture(scope="module")
def test_mobile():
    return f"9{int(time.time()) % 1000000000:09d}"  # 10-digit


def test_guest_join_missing_fields(published_exam):
    eid = published_exam["id"]
    r = requests.post(f"{API}/public/exam/{eid}/join", json={"name": "x"}, timeout=10)
    assert r.status_code == 400
    r2 = requests.post(f"{API}/public/exam/{eid}/join", json={"mobile": "9999999999"}, timeout=10)
    assert r2.status_code == 400


def test_guest_join_creates_student_and_returns_token(published_exam, test_mobile):
    eid = published_exam["id"]
    payload = {"name": "TEST_iter13_Guest", "mobile": test_mobile, "class_level": "12th"}
    r = requests.post(f"{API}/public/exam/{eid}/join", json=payload, timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["role"] == "student"
    assert data["token"]
    assert data["user"]["mobile"] == test_mobile
    assert data["user"]["username"].startswith("g")
    assert data["user"]["signup_status"] == "approved"
    assert data["user"]["signup_mode"] == "guest_link"
    # password_hash MUST not leak
    assert "password_hash" not in data["user"]
    # Credentials show auto-generated password (last 6 of mobile)
    assert data["credentials"]["username"] == "g" + test_mobile
    assert data["credentials"]["password"] == test_mobile[-6:]
    # exam appears in student exam_ids
    assert eid in (data["user"].get("exam_ids") or [])


def test_guest_token_lists_joined_exam(published_exam, test_mobile):
    eid = published_exam["id"]
    # Re-join (idempotent) to grab a fresh token
    r = requests.post(
        f"{API}/public/exam/{eid}/join",
        json={"name": "TEST_iter13_Guest", "mobile": test_mobile},
        timeout=15,
    )
    assert r.status_code == 200
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    rl = requests.get(f"{API}/exams", headers=headers, timeout=10)
    assert rl.status_code == 200
    ids = [e["id"] for e in rl.json()]
    assert eid in ids, f"Joined exam {eid} not visible to guest student"


def test_guest_rejoin_idempotent_same_username(published_exam, test_mobile):
    eid = published_exam["id"]
    r1 = requests.post(f"{API}/public/exam/{eid}/join",
                       json={"name": "TEST_iter13_Guest", "mobile": test_mobile}, timeout=15)
    r2 = requests.post(f"{API}/public/exam/{eid}/join",
                       json={"name": "TEST_iter13_Guest_v2", "mobile": test_mobile}, timeout=15)
    assert r1.status_code == 200 and r2.status_code == 200
    u1 = r1.json()["user"]
    u2 = r2.json()["user"]
    assert u1["id"] == u2["id"]
    assert u1["username"] == u2["username"]
    # Re-join returns password as None (not auto-regenerated)
    assert r2.json()["credentials"]["password"] in (None, "")


def test_guest_join_unpublished_exam_returns_403(admin_headers):
    """Create an unpublished exam then try to public-join."""
    # Make a quick unpublished exam
    payload = {
        "name": "TEST_iter13_unpublished",
        "description": "x",
        "duration_minutes": 10,
        "passing_marks": 0,
        "is_published": False,
        "negative_marking": False,
        "question_ids": [],
        "assigned_student_ids": [],
    }
    r = requests.post(f"{API}/exams", headers=admin_headers, json=payload, timeout=10)
    if r.status_code != 200:
        pytest.skip(f"Could not create unpublished exam: {r.status_code} {r.text[:100]}")
    eid = r.json()["id"]
    try:
        # Preview should 404
        rp = requests.get(f"{API}/public/exam/{eid}", timeout=10)
        assert rp.status_code == 404
        # Join should 403
        rj = requests.post(f"{API}/public/exam/{eid}/join",
                           json={"name": "x", "mobile": "9000000001"}, timeout=10)
        assert rj.status_code == 403
    finally:
        requests.delete(f"{API}/exams/{eid}", headers=admin_headers, timeout=10)
