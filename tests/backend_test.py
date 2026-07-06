"""Backend pytest suite for Gyansai Maths IIT Center API.

Covers: auth, dashboard, institute settings, student CRUD, question bank + meta + bulk-save,
exam CRUD + clone + publish, student attempt flow (start/save/violation/submit),
result + public result + certificate PDF, test series checkout, OCR via Emergent LLM key.
"""
import base64
import io
import os
import time
import pytest
import requests
from PIL import Image, ImageDraw, ImageFont

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://iit-test-portal.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@gyansai.com"
ADMIN_PASS = "admin123"
DEMO_USER = "demo"
DEMO_PASS = "demo123"


# ---------- Fixtures ----------
@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(f"{API}/auth/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=30)
    assert r.status_code == 200, f"admin login failed: {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="session")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="session")
def student_token():
    r = requests.post(f"{API}/auth/student/login", json={"username": DEMO_USER, "password": DEMO_PASS}, timeout=30)
    assert r.status_code == 200, f"student login failed: {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="session")
def student_headers(student_token):
    return {"Authorization": f"Bearer {student_token}"}


# ---------- Auth ----------
class TestAuth:
    def test_admin_login_success(self):
        r = requests.post(f"{API}/auth/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "admin"
        assert isinstance(body["token"], str) and len(body["token"]) > 10
        assert body["user"]["email"] == ADMIN_EMAIL

    def test_student_login_success(self):
        r = requests.post(f"{API}/auth/student/login", json={"username": DEMO_USER, "password": DEMO_PASS})
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "student"
        assert body["user"]["username"] == DEMO_USER

    def test_student_login_wrong_password(self):
        r = requests.post(f"{API}/auth/student/login", json={"username": DEMO_USER, "password": "wrong"})
        assert r.status_code == 401

    def test_me_endpoint(self, admin_headers):
        r = requests.get(f"{API}/auth/me", headers=admin_headers)
        assert r.status_code == 200
        assert r.json().get("role") == "admin"


# ---------- Dashboard ----------
class TestDashboard:
    def test_admin_dashboard(self, admin_headers):
        r = requests.get(f"{API}/admin/dashboard", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        for k in ["kpis", "revenue_chart", "student_growth", "exam_performance", "recent_activities", "live_attempts"]:
            assert k in data, f"missing {k}"
        assert "total_students" in data["kpis"]


# ---------- Institute Settings ----------
class TestInstituteSettings:
    def test_get_settings_public_admin(self, admin_headers):
        r = requests.get(f"{API}/admin/settings", headers=admin_headers)
        assert r.status_code == 200

    def test_update_settings_persists(self, admin_headers):
        new_name = "Gyansai Maths IIT Center"
        new_tag = f"TEST tagline {int(time.time())}"
        r = requests.put(f"{API}/admin/settings",
                         headers=admin_headers,
                         json={"name": new_name, "tagline": new_tag})
        assert r.status_code == 200, r.text
        assert r.json()["tagline"] == new_tag
        # verify persistence
        g = requests.get(f"{API}/admin/settings", headers=admin_headers).json()
        assert g["tagline"] == new_tag
        assert g["name"] == new_name


# ---------- Students CRUD ----------
class TestStudents:
    def test_create_duplicate_update_delete(self, admin_headers):
        uname = f"TEST_stu_{int(time.time())}"
        # create
        r = requests.post(f"{API}/admin/students", headers=admin_headers,
                          json={"name": "TEST Student", "username": uname, "password": "pass123"})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        assert r.json()["username"] == uname

        # duplicate
        r2 = requests.post(f"{API}/admin/students", headers=admin_headers,
                           json={"name": "Dup", "username": uname})
        assert r2.status_code == 400

        # update
        ru = requests.put(f"{API}/admin/students/{sid}", headers=admin_headers,
                          json={"name": "TEST Updated"})
        assert ru.status_code == 200
        assert ru.json()["name"] == "TEST Updated"

        # list contains updated
        rl = requests.get(f"{API}/admin/students", headers=admin_headers, params={"q": uname})
        assert rl.status_code == 200
        assert any(s["id"] == sid for s in rl.json())

        # delete
        rd = requests.delete(f"{API}/admin/students/{sid}", headers=admin_headers)
        assert rd.status_code == 200
        # confirm gone
        rl2 = requests.get(f"{API}/admin/students", headers=admin_headers, params={"q": uname})
        assert all(s["id"] != sid for s in rl2.json())


# ---------- Question Bank ----------
class TestQuestions:
    def test_meta(self, admin_headers):
        r = requests.get(f"{API}/questions/meta", headers=admin_headers)
        assert r.status_code == 200
        d = r.json()
        for k in ["subjects", "chapters", "topics", "total"]:
            assert k in d

    def test_crud_and_filter(self, admin_headers):
        payload = {
            "title": "TEST Q what is 2+2?",
            "subject": "Mathematics",
            "chapter": "Arithmetic",
            "topic": "Addition",
            "difficulty": "easy",
            "type": "mcq_single",
            "options": [{"key": "A", "text": "3"}, {"key": "B", "text": "4"}],
            "correct_answer": "B",
            "explanation": "2+2=4",
        }
        r = requests.post(f"{API}/questions", headers=admin_headers, json=payload)
        assert r.status_code == 200, r.text
        qid = r.json()["id"]

        # filter
        rl = requests.get(f"{API}/questions", headers=admin_headers, params={"subject": "Mathematics", "q": "TEST Q"})
        assert rl.status_code == 200
        assert any(q["id"] == qid for q in rl.json())

        # update
        ru = requests.put(f"{API}/questions/{qid}", headers=admin_headers, json={**payload, "title": "TEST Q updated"})
        assert ru.status_code == 200
        assert ru.json()["title"] == "TEST Q updated"

        # delete
        rd = requests.delete(f"{API}/questions/{qid}", headers=admin_headers)
        assert rd.status_code == 200

    def test_bulk_save(self, admin_headers):
        items = [
            {"title": "TEST bulk q1", "subject": "Physics", "options": [{"key": "A", "text": "x"}], "correct_answer": "A"},
            {"title": "TEST bulk q2", "subject": "Chemistry", "options": [{"key": "A", "text": "y"}], "correct_answer": "A"},
        ]
        r = requests.post(f"{API}/questions/bulk-save", headers=admin_headers, json={"questions": items})
        assert r.status_code == 200
        assert r.json()["saved"] == 2


# ---------- Exams ----------
@pytest.fixture(scope="session")
def seeded_exam_and_questions(admin_headers):
    """Find the seeded JEE Main exam (which has question_ids)."""
    r = requests.get(f"{API}/exams", headers=admin_headers)
    assert r.status_code == 200
    exams = r.json()
    target = next((e for e in exams if "JEE" in e.get("name", "")), None)
    assert target, "Seeded exam not found"
    # ensure published
    requests.put(f"{API}/exams/{target['id']}", headers=admin_headers, json={**{k: v for k, v in target.items() if k in [
        "name", "description", "type", "duration_minutes", "start_at", "end_at", "passing_marks",
        "instructions", "randomize", "negative_marking", "question_ids", "allowed_tab_switches",
        "enable_webcam", "price"
    ]}, "is_published": True})
    return target


class TestExamCRUD:
    def test_exam_list(self, admin_headers):
        r = requests.get(f"{API}/exams", headers=admin_headers)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_exam_create_update_clone_delete(self, admin_headers):
        payload = {
            "name": "TEST Exam CRUD",
            "description": "test",
            "duration_minutes": 30,
            "question_ids": [],
            "is_published": False,
        }
        r = requests.post(f"{API}/exams", headers=admin_headers, json=payload)
        assert r.status_code == 200
        eid = r.json()["id"]

        # update / publish
        upd = {**payload, "is_published": True, "name": "TEST Exam Updated"}
        ru = requests.put(f"{API}/exams/{eid}", headers=admin_headers, json=upd)
        assert ru.status_code == 200
        assert ru.json()["is_published"] is True

        # clone
        rc = requests.post(f"{API}/exams/{eid}/clone", headers=admin_headers)
        assert rc.status_code == 200
        cid = rc.json()["id"]
        assert cid != eid
        assert "(Copy)" in rc.json()["name"]

        # delete both
        assert requests.delete(f"{API}/exams/{eid}", headers=admin_headers).status_code == 200
        assert requests.delete(f"{API}/exams/{cid}", headers=admin_headers).status_code == 200


# ---------- Student Attempt Flow (requires fresh student to avoid one-shot lock) ----------
@pytest.fixture(scope="session")
def fresh_student_token(admin_headers, seeded_exam_and_questions):
    """Create fresh student with exam assigned, then log in to get token."""
    uname = f"TEST_attempt_{int(time.time())}"
    pw = "pass1234"
    r = requests.post(f"{API}/admin/students", headers=admin_headers,
                      json={"name": "TEST Attempt", "username": uname, "password": pw})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    # assign the exam
    eid = seeded_exam_and_questions["id"]
    ra = requests.post(f"{API}/admin/students/{sid}/assign", headers=admin_headers, json={"exam_ids": [eid]})
    assert ra.status_code == 200
    # login
    rl = requests.post(f"{API}/auth/student/login", json={"username": uname, "password": pw})
    assert rl.status_code == 200
    return {"token": rl.json()["token"], "id": sid, "username": uname, "exam_id": eid}


class TestAttemptFlow:
    def test_start_save_violation_submit_result(self, fresh_student_token, seeded_exam_and_questions):
        eid = seeded_exam_and_questions["id"]
        headers = {"Authorization": f"Bearer {fresh_student_token['token']}"}

        # start
        r = requests.post(f"{API}/exams/start", headers=headers, json={"exam_id": eid})
        assert r.status_code == 200, r.text
        attempt = r.json()
        aid = attempt["id"]
        qs = attempt["questions"]
        assert len(qs) > 0
        # questions must NOT include correct_answer/explanation
        for q in qs:
            assert "correct_answer" not in q
            assert "explanation" not in q

        # save first question (answer A — may be right or wrong)
        first_q = qs[0]
        rsave = requests.post(f"{API}/exams/save", headers=headers,
                              json={"attempt_id": aid, "question_id": first_q["id"], "answer": "A", "status": "answered"})
        assert rsave.status_code == 200

        # save a second one if available
        if len(qs) > 1:
            requests.post(f"{API}/exams/save", headers=headers,
                          json={"attempt_id": aid, "question_id": qs[1]["id"], "answer": "B", "status": "answered"})

        # violation: tab_switch (allowed=3 by default, should not auto-submit)
        rv = requests.post(f"{API}/exams/violation", headers=headers,
                           json={"attempt_id": aid, "violation_type": "tab_switch"})
        assert rv.status_code == 200
        body = rv.json()
        assert body["tab_switches"] >= 1
        # Likely auto_submit False unless allowed=1
        # submit
        rs = requests.post(f"{API}/exams/submit", headers=headers, json={"attempt_id": aid})
        assert rs.status_code == 200, rs.text
        result = rs.json()
        assert result["status"] == "submitted"
        for k in ["score", "correct", "wrong", "skipped", "subject_stats", "per_question"]:
            assert k in result, f"missing {k}"
        # per_question should expose correct_answer & explanation after submit
        assert "correct_answer" in result["per_question"][0]

        # result endpoint
        rr = requests.get(f"{API}/exams/result/{aid}", headers=headers)
        assert rr.status_code == 200
        rj = rr.json()
        assert "rank" in rj and "leaderboard" in rj and "accuracy" in rj

        # public result
        pr = requests.get(f"{API}/exams/public/result/{aid}")
        assert pr.status_code == 200
        pjson = pr.json()
        assert "score" in pjson
        # should NOT expose per_question or answers
        assert "per_question" not in pjson
        assert "answers" not in pjson

        # certificate PDF
        cert = requests.get(f"{API}/public/certificate/{aid}")
        assert cert.status_code == 200
        assert "application/pdf" in cert.headers.get("content-type", "")
        assert cert.content[:4] == b"%PDF"

        paper = requests.get(f"{API}/exams/result/{aid}/paper", headers=headers)
        assert paper.status_code == 200
        assert "application/pdf" in paper.headers.get("content-type", "")
        assert paper.content[:4] == b"%PDF"

        # store attempt_id for next tests
        TestAttemptFlow.attempt_id = aid


# ---------- Test Series Checkout (mocked) ----------
class TestCheckout:
    def test_checkout_grants_exam_access(self, admin_headers):
        # find a test series
        rs = requests.get(f"{API}/admin/test-series", headers=admin_headers)
        assert rs.status_code == 200
        series_list = rs.json()
        if not series_list:
            pytest.skip("No test series seeded")
        ts = series_list[0]

        # create a fresh student and login
        uname = f"TEST_checkout_{int(time.time())}"
        pw = "pass1234"
        rc = requests.post(f"{API}/admin/students", headers=admin_headers,
                           json={"name": "TEST Checkout", "username": uname, "password": pw})
        assert rc.status_code == 200
        sid = rc.json()["id"]
        rl = requests.post(f"{API}/auth/student/login", json={"username": uname, "password": pw})
        token = rl.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        # checkout
        rco = requests.post(f"{API}/student/checkout", headers=headers,
                            json={"item_type": "test_series", "item_id": ts["id"], "coupon": "GYAN10"})
        assert rco.status_code == 200, rco.text
        body = rco.json()
        assert body["mocked"] is True
        assert body["payment"]["status"] == "success"

        # verify access
        prof = requests.get(f"{API}/student/profile", headers=headers).json()
        for eid in ts.get("exam_ids", []):
            assert eid in (prof.get("exam_ids") or []), f"exam {eid} not granted"

        # cleanup
        requests.delete(f"{API}/admin/students/{sid}", headers=admin_headers)


# ---------- OCR ----------
def _make_question_image_b64() -> str:
    """Render a clear printed math question image and return base64 PNG."""
    img = Image.new("RGB", (900, 350), "white")
    d = ImageDraw.Draw(img)
    try:
        font_big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except Exception:
        font_big = ImageFont.load_default()
        font_sm = ImageFont.load_default()
    d.text((30, 20), "Q1. What is the value of 2 + 3 * 4 ?", fill="black", font=font_big)
    d.text((60, 90), "(A) 20", fill="black", font=font_sm)
    d.text((60, 130), "(B) 14", fill="black", font=font_sm)
    d.text((60, 170), "(C) 24", fill="black", font=font_sm)
    d.text((60, 210), "(D) 11", fill="black", font=font_sm)
    # add some shapes for visual variety
    d.rectangle([20, 10, 880, 260], outline="black", width=2)
    d.line([(30, 270), (870, 270)], fill="black", width=1)
    d.text((30, 285), "Subject: Mathematics  |  Topic: Order of operations", fill="black", font=font_sm)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


class TestOCR:
    def test_ocr_extracts_questions(self, admin_headers):
        b64 = _make_question_image_b64()
        r = requests.post(f"{API}/questions/ocr", headers=admin_headers,
                          json={"image_base64": b64, "mime_type": "image/png"}, timeout=120)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "questions" in data
        assert isinstance(data["questions"], list)
        # The LLM should extract at least 1 question from the clear printed image
        assert len(data["questions"]) >= 1, f"OCR returned no questions: {data}"
        q = data["questions"][0]
        assert "title" in q
        assert q.get("type")
        assert q.get("subject")


# ---------- Schedule Enforcement (Live Exam Scheduling) ----------
from datetime import datetime, timezone, timedelta


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture(scope="class")
def scheduled_exam_setup(admin_headers):
    """Create a fresh published exam + fresh student assigned to it for schedule tests."""
    # find some question ids
    rq = requests.get(f"{API}/questions", headers=admin_headers, params={"limit": 5})
    assert rq.status_code == 200
    qids = [q["id"] for q in rq.json()[:3]]
    assert qids, "Need at least 1 seeded question"
    # exam
    payload = {
        "name": f"TEST Scheduled Exam {int(time.time())}",
        "duration_minutes": 30,
        "question_ids": qids,
        "is_published": True,
    }
    r = requests.post(f"{API}/exams", headers=admin_headers, json=payload)
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    # fresh student
    uname = f"TEST_sched_{int(time.time())}"
    pw = "pass1234"
    rs = requests.post(f"{API}/admin/students", headers=admin_headers,
                       json={"name": "TEST Sched", "username": uname, "password": pw})
    assert rs.status_code == 200, rs.text
    sid = rs.json()["id"]
    requests.post(f"{API}/admin/students/{sid}/assign", headers=admin_headers, json={"exam_ids": [eid]})
    rl = requests.post(f"{API}/auth/student/login", json={"username": uname, "password": pw})
    token = rl.json()["token"]
    yield {"exam_id": eid, "student_id": sid, "headers": {"Authorization": f"Bearer {token}"},
           "base_payload": payload}
    # cleanup
    requests.delete(f"{API}/exams/{eid}", headers=admin_headers)
    requests.delete(f"{API}/admin/students/{sid}", headers=admin_headers)


class TestExamScheduling:
    """Live exam scheduling — start_at / end_at enforcement on /api/exams/start."""

    def _update_window(self, admin_headers, eid, base_payload, start_at, end_at):
        payload = {**base_payload, "start_at": start_at, "end_at": end_at}
        r = requests.put(f"{API}/exams/{eid}", headers=admin_headers, json=payload)
        assert r.status_code == 200, r.text

    def test_start_at_in_future_returns_403(self, admin_headers, scheduled_exam_setup):
        s = scheduled_exam_setup
        future = _iso_utc(datetime.now(timezone.utc) + timedelta(days=1))
        self._update_window(admin_headers, s["exam_id"], s["base_payload"], future, None)
        r = requests.post(f"{API}/exams/start", headers=s["headers"], json={"exam_id": s["exam_id"]})
        assert r.status_code == 403, r.text
        assert "opens at" in (r.json().get("detail") or "").lower()

    def test_end_at_in_past_returns_403(self, admin_headers, scheduled_exam_setup):
        s = scheduled_exam_setup
        past = _iso_utc(datetime.now(timezone.utc) - timedelta(days=1))
        self._update_window(admin_headers, s["exam_id"], s["base_payload"], None, past)
        r = requests.post(f"{API}/exams/start", headers=s["headers"], json={"exam_id": s["exam_id"]})
        assert r.status_code == 403, r.text
        assert "closed" in (r.json().get("detail") or "").lower()

    def test_within_window_returns_200(self, admin_headers, scheduled_exam_setup):
        s = scheduled_exam_setup
        start = _iso_utc(datetime.now(timezone.utc) - timedelta(hours=1))
        end = _iso_utc(datetime.now(timezone.utc) + timedelta(hours=1))
        self._update_window(admin_headers, s["exam_id"], s["base_payload"], start, end)
        r = requests.post(f"{API}/exams/start", headers=s["headers"], json={"exam_id": s["exam_id"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("status") == "in_progress"
        assert body.get("exam_id") == s["exam_id"]
        assert "id" in body


# ---------- Manual Evaluation (Subjective Q grading) ----------
@pytest.fixture(scope="class")
def subjective_attempt_setup(admin_headers):
    """Create a subjective question, exam containing it, fresh student, attempt, submit."""
    # 1. Create a 'long' question with marks=10
    qpayload = {
        "title": "TEST Explain Newton's Second Law",
        "subject": "Physics",
        "type": "long",
        "marks": 10,
        "correct_answer": "F = m*a; force equals mass times acceleration",
        "explanation": "Model answer expects F=ma derivation",
    }
    rq = requests.post(f"{API}/questions", headers=admin_headers, json=qpayload)
    assert rq.status_code == 200, rq.text
    qid = rq.json()["id"]

    # 2. Create published exam with only this question
    epayload = {
        "name": f"TEST Subjective Exam {int(time.time())}",
        "duration_minutes": 30,
        "question_ids": [qid],
        "is_published": True,
    }
    re = requests.post(f"{API}/exams", headers=admin_headers, json=epayload)
    assert re.status_code == 200, re.text
    eid = re.json()["id"]

    # 3. Fresh student, assigned
    uname = f"TEST_subj_{int(time.time())}"
    pw = "pass1234"
    rs = requests.post(f"{API}/admin/students", headers=admin_headers,
                       json={"name": "TEST Subj", "username": uname, "password": pw})
    sid = rs.json()["id"]
    requests.post(f"{API}/admin/students/{sid}/assign", headers=admin_headers, json={"exam_ids": [eid]})
    rl = requests.post(f"{API}/auth/student/login", json={"username": uname, "password": pw})
    stoken = rl.json()["token"]
    sheaders = {"Authorization": f"Bearer {stoken}"}

    # 4. Start attempt, save answer, submit
    rst = requests.post(f"{API}/exams/start", headers=sheaders, json={"exam_id": eid})
    assert rst.status_code == 200, rst.text
    attempt = rst.json()
    aid = attempt["id"]
    sa = requests.post(f"{API}/exams/save", headers=sheaders,
                      json={"attempt_id": aid, "question_id": qid,
                            "answer": "Force equals mass times acceleration", "status": "answered"})
    assert sa.status_code == 200
    sb = requests.post(f"{API}/exams/submit", headers=sheaders, json={"attempt_id": aid})
    assert sb.status_code == 200, sb.text

    yield {
        "qid": qid, "eid": eid, "sid": sid, "aid": aid,
        "submitted": sb.json(),
        "student_headers": sheaders,
        "student_token": stoken,
    }
    # cleanup
    requests.delete(f"{API}/exams/{eid}", headers=admin_headers)
    requests.delete(f"{API}/questions/{qid}", headers=admin_headers)
    requests.delete(f"{API}/admin/students/{sid}", headers=admin_headers)


class TestManualEvaluation:
    """Manual evaluation flow for subjective questions (short/long/file)."""

    def test_submit_marks_subjective_as_pending(self, subjective_attempt_setup):
        s = subjective_attempt_setup
        body = s["submitted"]
        assert body["status"] == "submitted"
        assert body.get("score") == 0
        assert body.get("max_score") == 10
        assert body.get("pending_review") == 1
        assert body.get("has_pending_review") is True
        pq = next((p for p in body.get("per_question", []) if p["qid"] == s["qid"]), None)
        assert pq is not None
        assert pq["result"] == "pending_review"
        assert pq["type"] == "long"
        assert pq.get("max_marks") == 10
        assert pq.get("marks") == 0

    def test_pending_list_includes_attempt(self, admin_headers, subjective_attempt_setup):
        s = subjective_attempt_setup
        r = requests.get(f"{API}/exams/evaluation/pending", headers=admin_headers)
        assert r.status_code == 200, r.text
        rows = r.json()
        ids = [a["id"] for a in rows]
        assert s["aid"] in ids
        row = next(a for a in rows if a["id"] == s["aid"])
        assert row.get("pending_review") == 1
        assert row.get("max_score") == 10

    def test_get_evaluation_details(self, admin_headers, subjective_attempt_setup):
        s = subjective_attempt_setup
        r = requests.get(f"{API}/exams/evaluation/{s['aid']}", headers=admin_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["attempt_id"] == s["aid"]
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["qid"] == s["qid"]
        assert item["type"] == "long"
        assert item["max_marks"] == 10
        assert item["current_marks"] == 0
        assert item["result"] == "pending_review"
        assert "Newton" in (item.get("title") or "")
        assert "Force equals mass" in (item.get("given") or "")
        assert item.get("model_answer") is not None  # F=m*a

    def test_non_admin_cannot_access_pending(self, subjective_attempt_setup):
        s = subjective_attempt_setup
        r = requests.get(f"{API}/exams/evaluation/pending", headers=s["student_headers"])
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"

    def test_evaluation_marks_exceed_max_returns_400(self, admin_headers, subjective_attempt_setup):
        s = subjective_attempt_setup
        r = requests.post(f"{API}/exams/evaluation/{s['aid']}", headers=admin_headers,
                          json={"evaluations": [{"qid": s["qid"], "marks": 99, "comment": "too high"}]})
        assert r.status_code == 400, r.text

    def test_save_evaluation_recomputes_score(self, admin_headers, subjective_attempt_setup):
        s = subjective_attempt_setup
        r = requests.post(f"{API}/exams/evaluation/{s['aid']}", headers=admin_headers,
                          json={"evaluations": [{"qid": s["qid"], "marks": 7, "comment": "good attempt"}]})
        assert r.status_code == 200, r.text
        updated = r.json()
        assert updated["score"] == 7
        assert updated["has_pending_review"] is False
        assert updated.get("pending_review") == 0
        pq = next(p for p in updated["per_question"] if p["qid"] == s["qid"])
        assert pq["marks"] == 7
        assert pq["comment"] == "good attempt"
        assert pq["result"] == "partial"

    def test_after_eval_not_in_pending_list(self, admin_headers, subjective_attempt_setup):
        s = subjective_attempt_setup
        r = requests.get(f"{API}/exams/evaluation/pending", headers=admin_headers)
        assert r.status_code == 200
        ids = [a["id"] for a in r.json()]
        assert s["aid"] not in ids


# ---------- Snapshot Persistence + Admin Attempts/Sharing (Iteration 3) ----------
import random as _rand


def _make_small_jpeg_b64(noise: bool = False) -> str:
    """Tiny valid JPEG, base64-encoded (no data URL prefix)."""
    img = Image.new("RGB", (60, 40), (120, 180, 60))
    if noise:
        d = ImageDraw.Draw(img)
        for _ in range(40):
            x, y = _rand.randint(0, 59), _rand.randint(0, 39)
            d.point((x, y), fill=(_rand.randint(0, 255), _rand.randint(0, 255), _rand.randint(0, 255)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


@pytest.fixture(scope="module")
def submitted_attempt_setup(admin_headers):
    """Create a fresh student, attempt the JEE exam, push a snapshot before submit, then submit.
    Returns dict with attempt_id, exam_id, student_id, student_headers."""
    # find seeded JEE exam (or any published with question_ids)
    rexams = requests.get(f"{API}/exams", headers=admin_headers)
    exams = rexams.json()
    target = next((e for e in exams if e.get("question_ids") and e.get("is_published")), None)
    if not target:
        # fallback: pick first with question_ids and publish it
        target = next((e for e in exams if e.get("question_ids")), None)
        assert target, "No exam with questions seeded"
        requests.put(f"{API}/exams/{target['id']}", headers=admin_headers, json={**{k: v for k, v in target.items() if k in [
            "name", "description", "type", "duration_minutes", "start_at", "end_at", "passing_marks",
            "instructions", "randomize", "negative_marking", "question_ids", "allowed_tab_switches",
            "enable_webcam", "price"
        ]}, "is_published": True})
    eid = target["id"]

    uname = f"TEST_snap_{int(time.time())}_{_rand.randint(100, 999)}"
    pw = "pass1234"
    rs = requests.post(f"{API}/admin/students", headers=admin_headers,
                       json={"name": "TEST Snap", "username": uname, "password": pw})
    assert rs.status_code == 200, rs.text
    sid = rs.json()["id"]
    requests.post(f"{API}/admin/students/{sid}/assign", headers=admin_headers, json={"exam_ids": [eid]})
    rl = requests.post(f"{API}/auth/student/login", json={"username": uname, "password": pw})
    stoken = rl.json()["token"]
    sh = {"Authorization": f"Bearer {stoken}"}

    # start attempt
    ra = requests.post(f"{API}/exams/start", headers=sh, json={"exam_id": eid})
    assert ra.status_code == 200, ra.text
    aid = ra.json()["id"]

    yield {"attempt_id": aid, "exam_id": eid, "student_id": sid,
           "student_headers": sh, "username": uname}

    # cleanup
    requests.delete(f"{API}/admin/students/{sid}", headers=admin_headers)


class TestSnapshotPersistence:
    """POST /api/exams/snapshot — image_base64 must persist in DB and not echo back."""

    def test_snapshot_basic_persists_and_strips_image(self, admin_headers, submitted_attempt_setup):
        s = submitted_attempt_setup
        b64 = _make_small_jpeg_b64(noise=True)
        r = requests.post(f"{API}/exams/snapshot", headers=s["student_headers"],
                          json={"attempt_id": s["attempt_id"], "image_base64": b64, "violation": None})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "image_base64" not in body, "image_base64 must be stripped from response"
        assert body["attempt_id"] == s["attempt_id"]
        assert body["size_bytes"] == len(b64)
        snap_id = body["id"]
        # Verify via admin snapshots endpoint that image_base64 was actually stored
        rs = requests.get(f"{API}/exams/admin/attempts/{s['attempt_id']}/snapshots", headers=admin_headers)
        assert rs.status_code == 200, rs.text
        snaps = rs.json()
        stored = next((x for x in snaps if x["id"] == snap_id), None)
        assert stored is not None, "Snapshot not persisted"
        assert stored.get("image_base64") == b64, "stored image_base64 differs from input"

    def test_snapshot_oversized_truncated_to_200KB(self, admin_headers, submitted_attempt_setup):
        s = submitted_attempt_setup
        oversized = "A" * 250_000  # ~250 KB string (not real image but server caps blindly)
        r = requests.post(f"{API}/exams/snapshot", headers=s["student_headers"],
                          json={"attempt_id": s["attempt_id"], "image_base64": oversized})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["size_bytes"] == 200_000, f"expected 200000, got {body['size_bytes']}"
        snap_id = body["id"]
        rs = requests.get(f"{API}/exams/admin/attempts/{s['attempt_id']}/snapshots", headers=admin_headers)
        assert rs.status_code == 200
        stored = next(x for x in rs.json() if x["id"] == snap_id)
        assert len(stored["image_base64"]) == 200_000

    def test_snapshot_data_url_prefix_stripped(self, admin_headers, submitted_attempt_setup):
        s = submitted_attempt_setup
        raw = _make_small_jpeg_b64(noise=True)
        dataurl = f"data:image/jpeg;base64,{raw}"
        r = requests.post(f"{API}/exams/snapshot", headers=s["student_headers"],
                          json={"attempt_id": s["attempt_id"], "image_base64": dataurl, "violation": "tab_switch"})
        assert r.status_code == 200, r.text
        body = r.json()
        snap_id = body["id"]
        assert body["size_bytes"] == len(raw), "prefix should be stripped before size measurement"
        rs = requests.get(f"{API}/exams/admin/attempts/{s['attempt_id']}/snapshots", headers=admin_headers)
        stored = next(x for x in rs.json() if x["id"] == snap_id)
        assert stored["image_base64"] == raw, "data URL prefix not stripped"
        assert not stored["image_base64"].startswith("data:"), "prefix still present"


class TestAdminAttemptsEndpoints:
    """GET /api/exams/admin/attempts (list, detail, snapshots) — Iteration 3."""

    def test_admin_attempts_list_sorted_with_violations_count(self, admin_headers, submitted_attempt_setup):
        s = submitted_attempt_setup
        r = requests.get(f"{API}/exams/admin/attempts", headers=admin_headers)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert isinstance(rows, list)
        assert any(x["id"] == s["attempt_id"] for x in rows), "attempt not in list"
        row = next(x for x in rows if x["id"] == s["attempt_id"])
        assert "violations_count" in row
        assert isinstance(row["violations_count"], int)
        assert "violations" not in row, "raw violations array should be stripped"

    def test_admin_attempts_list_filters(self, admin_headers, submitted_attempt_setup):
        s = submitted_attempt_setup
        # filter by exam_id
        r = requests.get(f"{API}/exams/admin/attempts", headers=admin_headers,
                         params={"exam_id": s["exam_id"]})
        assert r.status_code == 200
        for x in r.json():
            assert x["exam_id"] == s["exam_id"]
        # filter by student_id
        r2 = requests.get(f"{API}/exams/admin/attempts", headers=admin_headers,
                          params={"student_id": s["student_id"]})
        assert r2.status_code == 200
        for x in r2.json():
            assert x["student_id"] == s["student_id"]
        # filter by status=in_progress
        r3 = requests.get(f"{API}/exams/admin/attempts", headers=admin_headers,
                         params={"status": "in_progress", "student_id": s["student_id"]})
        assert r3.status_code == 200
        for x in r3.json():
            assert x["status"] == "in_progress"

    def test_admin_attempts_list_forbidden_for_student(self, submitted_attempt_setup):
        s = submitted_attempt_setup
        r = requests.get(f"{API}/exams/admin/attempts", headers=s["student_headers"])
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"

    def test_admin_attempt_detail_in_progress(self, admin_headers, submitted_attempt_setup):
        s = submitted_attempt_setup
        r = requests.get(f"{API}/exams/admin/attempts/{s['attempt_id']}", headers=admin_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == s["attempt_id"]
        assert "snapshots_count" in body
        assert body["snapshots_count"] >= 1  # we pushed snapshots above
        # in_progress -> no rank/leaderboard
        assert body["status"] == "in_progress"

    def test_admin_attempt_snapshots_ordered_ascending(self, admin_headers, submitted_attempt_setup):
        s = submitted_attempt_setup
        r = requests.get(f"{API}/exams/admin/attempts/{s['attempt_id']}/snapshots", headers=admin_headers)
        assert r.status_code == 200, r.text
        snaps = r.json()
        assert len(snaps) >= 1
        ats = [x["at"] for x in snaps]
        assert ats == sorted(ats), "snapshots not sorted ascending by 'at'"
        for sn in snaps:
            assert "image_base64" in sn

    def test_admin_attempt_detail_404_for_missing(self, admin_headers):
        r = requests.get(f"{API}/exams/admin/attempts/does-not-exist", headers=admin_headers)
        assert r.status_code == 404


class TestAdminShareEndpoint:
    """POST /api/exams/admin/attempts/{id}/share — Iteration 3."""

    def test_share_not_submitted_returns_400(self, admin_headers, submitted_attempt_setup):
        s = submitted_attempt_setup
        r = requests.post(f"{API}/exams/admin/attempts/{s['attempt_id']}/share",
                          headers=admin_headers,
                          json={"channel": "whatsapp", "recipient": "+919876543210"})
        assert r.status_code == 400, r.text
        assert "not yet submitted" in (r.json().get("detail") or "").lower()

    def test_share_non_admin_403(self, submitted_attempt_setup):
        s = submitted_attempt_setup
        r = requests.post(f"{API}/exams/admin/attempts/{s['attempt_id']}/share",
                          headers=s["student_headers"],
                          json={"channel": "whatsapp"})
        assert r.status_code == 403, r.text

    def test_share_after_submit_returns_links_and_template(self, admin_headers, submitted_attempt_setup):
        s = submitted_attempt_setup
        # submit the attempt first
        rsub = requests.post(f"{API}/exams/submit", headers=s["student_headers"],
                             json={"attempt_id": s["attempt_id"]})
        assert rsub.status_code == 200, rsub.text

        # detail now includes rank/leaderboard/accuracy/snapshots_count
        rd = requests.get(f"{API}/exams/admin/attempts/{s['attempt_id']}", headers=admin_headers)
        assert rd.status_code == 200
        det = rd.json()
        assert det["status"] == "submitted"
        for k in ["rank", "total_participants", "leaderboard", "accuracy", "snapshots_count"]:
            assert k in det, f"missing {k} in admin attempt detail"
        assert det["snapshots_count"] >= 2  # we pushed multiple snapshots

        # share
        r = requests.post(f"{API}/exams/admin/attempts/{s['attempt_id']}/share",
                          headers=admin_headers,
                          json={"channel": "whatsapp", "recipient": "+919876543210"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["channel"] == "whatsapp"
        assert body["recipient"] == "+919876543210"
        assert body["attempt_id"] == s["attempt_id"]
        assert body["public_path"] == f"/r/{s['attempt_id']}"
        assert body["certificate_path"] == f"/api/public/certificate/{s['attempt_id']}"
        assert "{base}" in body["message_template"]
        assert body["public_path"] in body["message_template"]
        assert body["certificate_path"] in body["message_template"]
        assert "id" in body and isinstance(body["id"], str)


# ---------- Parent-Accessible Recording + Result (Iteration 4) ----------
# Uses pre-existing submitted attempt with snapshots: c18eb0a0-8164-4e01-8a3b-fe977ee35df5
PARENT_ATTEMPT_ID = "c18eb0a0-8164-4e01-8a3b-fe977ee35df5"


class TestParentPublicRecording:
    """/api/public/recording/{id} — no auth, returns snapshot images for submitted attempts."""

    def test_recording_submitted_returns_snapshots_with_images_sorted(self):
        r = requests.get(f"{API}/public/recording/{PARENT_ATTEMPT_ID}")
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ["attempt_id", "student_name", "exam_name", "snapshots"]:
            assert k in body, f"missing {k}"
        assert body["attempt_id"] == PARENT_ATTEMPT_ID
        assert isinstance(body["snapshots"], list)
        assert len(body["snapshots"]) >= 1, "expected at least 1 snapshot"
        # Each snapshot has image_base64
        for sn in body["snapshots"]:
            assert "id" in sn and "at" in sn
            assert "image_base64" in sn and isinstance(sn["image_base64"], str) and len(sn["image_base64"]) > 0
        # Sorted ascending by 'at'
        ats = [sn["at"] for sn in body["snapshots"]]
        assert ats == sorted(ats), "snapshots not sorted ascending by 'at'"

    def test_recording_unknown_id_404(self):
        r = requests.get(f"{API}/public/recording/does-not-exist-xyz-iter4")
        assert r.status_code == 404
        assert "Recording not available" in (r.json().get("detail") or "")

    def test_recording_in_progress_attempt_404(self, admin_headers, submitted_attempt_setup):
        """submitted_attempt_setup yields an in_progress attempt (submit happens later in another class).
        We can only safely call this before the share-test class submits it. Order matters by file layout:
        TestSnapshotPersistence and TestAdminAttemptsEndpoints don't submit; TestAdminShareEndpoint does.
        Since this test runs after that class (pytest collects by file order), use a NEW in_progress attempt."""
        # create a fresh in_progress attempt to avoid dependency on collection order
        rexams = requests.get(f"{API}/exams", headers=admin_headers)
        target = next((e for e in rexams.json() if e.get("question_ids") and e.get("is_published")), None)
        assert target, "no published exam with questions"
        uname = f"TEST_inprog_{int(time.time())}_{_rand.randint(100, 999)}"
        rs = requests.post(f"{API}/admin/students", headers=admin_headers,
                           json={"name": "TEST InProg", "username": uname, "password": "pass1234"})
        sid = rs.json()["id"]
        try:
            requests.post(f"{API}/admin/students/{sid}/assign", headers=admin_headers,
                          json={"exam_ids": [target["id"]]})
            rl = requests.post(f"{API}/auth/student/login",
                               json={"username": uname, "password": "pass1234"})
            sh = {"Authorization": f"Bearer {rl.json()['token']}"}
            ra = requests.post(f"{API}/exams/start", headers=sh, json={"exam_id": target["id"]})
            assert ra.status_code == 200
            aid = ra.json()["id"]
            r = requests.get(f"{API}/public/recording/{aid}")
            assert r.status_code == 404, r.text
            assert "Recording not available" in (r.json().get("detail") or "")
        finally:
            requests.delete(f"{API}/admin/students/{sid}", headers=admin_headers)


class TestParentPublicResult:
    """/api/public/result/{id} — now also includes violations[] and snapshots_count."""

    def test_result_includes_violations_and_snapshots_count(self):
        r = requests.get(f"{API}/public/result/{PARENT_ATTEMPT_ID}")
        assert r.status_code == 200, r.text
        body = r.json()
        # core fields
        for k in ["id", "exam_name", "student_name", "score", "max_score",
                  "violations", "violations_count", "snapshots_count", "tab_switches"]:
            assert k in body, f"missing {k}"
        # violations is a list of {type, at}
        assert isinstance(body["violations"], list)
        if body["violations"]:
            v = body["violations"][0]
            assert "type" in v and "at" in v
            # No sensitive payload fields beyond type/at
            assert set(v.keys()) == {"type", "at"}
        assert isinstance(body["snapshots_count"], int)
        assert body["snapshots_count"] >= 1
        # must NOT leak answers
        assert "per_question" not in body
        assert "answers" not in body

    def test_result_unknown_id_404(self):
        r = requests.get(f"{API}/public/result/does-not-exist-xyz-iter4")
        assert r.status_code == 404


class TestLegacyPublicEndpoints:
    """Backward compatibility: legacy /api/exams/public/* paths must still respond 200."""

    def test_legacy_public_result_200(self):
        r = requests.get(f"{API}/exams/public/result/{PARENT_ATTEMPT_ID}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == PARENT_ATTEMPT_ID
        assert "score" in body

    def test_legacy_public_recording_200(self):
        r = requests.get(f"{API}/exams/public/recording/{PARENT_ATTEMPT_ID}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["attempt_id"] == PARENT_ATTEMPT_ID
        assert isinstance(body["snapshots"], list)
        assert len(body["snapshots"]) >= 1
        assert "image_base64" in body["snapshots"][0]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
