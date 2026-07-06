"""Iter12 — v2 features end-to-end test
Covers: Batches CRUD, Teacher login, Student signup approval gate, Public batches,
Exam clone/import-from-bank/share, Attendance, Study Material, Notifications
(in-app + MOCKED email/sms/whatsapp), exam visibility by batch_ids."""
import os
import time
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE:
    # Fallback for backend-side run
    BASE = "http://localhost:8001"
API = f"{BASE}/api"

ADMIN_EMAIL = "admin@gyansai.com"
ADMIN_PASS = "admin123"
DEMO_USER = "demo"
DEMO_PASS = "demo123"

TS = int(time.time())


# ---------------- Fixtures ----------------
@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_h(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def demo_token():
    r = requests.post(f"{API}/auth/student/login", json={"username": DEMO_USER, "password": DEMO_PASS}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def demo_h(demo_token):
    return {"Authorization": f"Bearer {demo_token}"}


# Cleanup tracker
_created = {"batches": [], "teachers": [], "students": [], "materials": [], "exams": []}


@pytest.fixture(scope="module", autouse=True)
def _cleanup(admin_h):
    yield
    for bid in _created["batches"]:
        requests.delete(f"{API}/batches/{bid}", headers=admin_h)
    for tid in _created["teachers"]:
        requests.delete(f"{API}/admin/teachers/{tid}", headers=admin_h)
    for sid in _created["students"]:
        requests.delete(f"{API}/admin/students/{sid}", headers=admin_h)
    for mid in _created["materials"]:
        requests.delete(f"{API}/admin/study-material/{mid}", headers=admin_h)
    for eid in _created["exams"]:
        requests.delete(f"{API}/exams/{eid}", headers=admin_h)


# ---------------- Health ----------------
def test_health():
    r = requests.get(f"{API}/health", timeout=10)
    assert r.status_code == 200


# ---------------- Batches CRUD ----------------
def test_batches_crud(admin_h):
    # Create
    payload = {"name": f"TEST_iter12_BatchA_{TS}", "class_level": "12th", "schedule": "Mon-Wed 6PM"}
    r = requests.post(f"{API}/batches", json=payload, headers=admin_h)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["name"] == payload["name"]
    assert "id" in b
    bid = b["id"]
    _created["batches"].append(bid)

    # List
    r = requests.get(f"{API}/batches", headers=admin_h)
    assert r.status_code == 200
    rows = r.json()
    assert any(x["id"] == bid for x in rows)
    assert all("student_count" in x for x in rows)

    # Update
    r = requests.put(f"{API}/batches/{bid}", json={"name": f"TEST_iter12_BatchA_upd_{TS}", "class_level": "12th", "schedule": "Tue-Thu 7PM"}, headers=admin_h)
    assert r.status_code == 200
    assert r.json()["schedule"] == "Tue-Thu 7PM"


def test_batches_public_no_auth():
    r = requests.get(f"{API}/public/batches", timeout=10)
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    # Should include the batch from previous test
    if rows:
        assert any(k in rows[0] for k in ("name", "class_level"))


# ---------------- Teacher ----------------
def test_teacher_create_and_login(admin_h):
    payload = {"name": "TEST_Sharma", "email": f"sharma_{TS}@gyansai.com", "password": "teacher123", "mobile": "9999900000", "subjects": ["Math"]}
    r = requests.post(f"{API}/admin/teachers", json=payload, headers=admin_h)
    assert r.status_code == 200, r.text
    t = r.json()
    _created["teachers"].append(t["id"])
    assert t["email"] == payload["email"].lower()
    assert "password_hash" not in t

    # Login
    r = requests.post(f"{API}/auth/teacher/login", json={"email": payload["email"], "password": "teacher123"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["role"] == "teacher"
    assert "token" in j

    # Bad password
    r2 = requests.post(f"{API}/auth/teacher/login", json={"email": payload["email"], "password": "wrong"})
    assert r2.status_code == 401


# ---------------- Student signup approval gate ----------------
def test_student_signup_pending_then_approved(admin_h):
    mobile = f"98{TS % 100000000:08d}"
    payload = {
        "name": "TEST_NewStudent",
        "mobile": mobile,
        "password": "newstud123",
        "class_level": "12th",
        "parent_mobile": "9090909090",
        "school": "Test School",
        "email": f"ns_{TS}@x.com",
        "batch_id": "",
    }
    r = requests.post(f"{API}/auth/signup", json=payload)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["auto_approved"] is False
    assert "username" in j
    uname = j["username"]

    # Find created student
    r = requests.get(f"{API}/admin/students", headers=admin_h)
    assert r.status_code == 200
    students = r.json()
    new_s = next((s for s in students if s.get("username") == uname), None)
    assert new_s is not None, "Signup student not found in admin list"
    sid = new_s["id"]
    _created["students"].append(sid)
    assert new_s.get("signup_status") == "pending"

    # Login should be blocked
    r = requests.post(f"{API}/auth/student/login", json={"username": uname, "password": "newstud123"})
    assert r.status_code == 403, r.text
    assert "pending" in r.text.lower() or "approv" in r.text.lower()

    # Approve via admin PUT
    r = requests.put(f"{API}/admin/students/{sid}", json={"signup_status": "approved", "status": "active"}, headers=admin_h)
    assert r.status_code == 200, r.text

    # Now login works
    r = requests.post(f"{API}/auth/student/login", json={"username": uname, "password": "newstud123"})
    assert r.status_code == 200, r.text
    assert "token" in r.json()


# ---------------- Exam clone / import / share ----------------
def _create_exam(admin_h, name, class_level="12th"):
    payload = {
        "name": name,
        "class_level": class_level,
        "duration_minutes": 60,
        "is_published": False,
        "assigned_student_ids": [],
    }
    r = requests.post(f"{API}/exams", json=payload, headers=admin_h)
    assert r.status_code == 200, r.text
    e = r.json()
    _created["exams"].append(e["id"])
    return e


def test_exam_create_independent(admin_h):
    e = _create_exam(admin_h, f"TEST_iter12_Exam_{TS}")
    # POST /api/exams creates exam with question_ids:[] by default
    assert e.get("question_ids", []) == []


def test_exam_clone_clears_assignments(admin_h):
    # Create source exam, assign demo student, add question stub
    # Get a question id from bank to seed
    rq = requests.get(f"{API}/questions?limit=1", headers=admin_h)
    qids = []
    if rq.status_code == 200 and rq.json():
        qids = [rq.json()[0]["id"]]
    src = _create_exam(admin_h, f"TEST_iter12_ExamSrc_{TS}")
    # Update with q + student
    rq2 = requests.get(f"{API}/admin/students", headers=admin_h).json()
    demo = next((s for s in rq2 if s.get("username") == DEMO_USER), None)
    assert demo
    requests.put(f"{API}/exams/{src['id']}", json={
        "name": src["name"], "class_level": "12th", "duration_minutes": 60,
        "is_published": True, "assigned_student_ids": [demo["id"]], "question_ids": qids,
    }, headers=admin_h)

    # Clone
    r = requests.post(f"{API}/exams/{src['id']}/clone", headers=admin_h)
    assert r.status_code == 200, r.text
    cloned = r.json()
    _created["exams"].append(cloned["id"])
    assert cloned["id"] != src["id"]
    assert cloned["is_published"] is False
    assert cloned.get("assigned_student_ids") == []
    # Questions carry over
    assert set(cloned.get("question_ids") or []) == set(qids)


def test_exam_import_from_bank_appends_no_duplicates(admin_h):
    e = _create_exam(admin_h, f"TEST_iter12_ImportTarget_{TS}")
    # Get 2 question ids
    rq = requests.get(f"{API}/questions?limit=3", headers=admin_h).json()
    if len(rq) < 2:
        pytest.skip("Not enough seeded questions to test import")
    qids = [q["id"] for q in rq[:2]]
    r = requests.post(f"{API}/exams/{e['id']}/import-from-bank", json={"question_ids": qids}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["question_count"] == 2

    # Import same again — no duplicates
    r2 = requests.post(f"{API}/exams/{e['id']}/import-from-bank", json={"question_ids": qids}, headers=admin_h)
    assert r2.status_code == 200
    assert r2.json()["question_count"] == 2


def test_exam_share(admin_h):
    e = _create_exam(admin_h, f"TEST_iter12_ShareEx_{TS}")
    r = requests.post(f"{API}/exams/{e['id']}/share", headers=admin_h)
    assert r.status_code == 200, r.text
    s = r.json()
    assert "url" in s and "whatsapp" in s and "email" in s and "message" in s
    assert f"join={e['id']}" in s["url"]
    assert s["whatsapp"].startswith("https://wa.me/")


# ---------------- Attendance ----------------
def test_attendance_mark_list_and_my_stats(admin_h, demo_h):
    # Find demo student id
    rq = requests.get(f"{API}/admin/students", headers=admin_h).json()
    demo = next((s for s in rq if s.get("username") == DEMO_USER), None)
    assert demo
    today = time.strftime("%Y-%m-%d")
    payload = {"date": today, "class_level": "12th", "entries": [{"student_id": demo["id"], "status": "present"}]}
    r = requests.post(f"{API}/attendance/mark", json=payload, headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["marked"] == 1

    # List
    r = requests.get(f"{API}/attendance?date={today}", headers=admin_h)
    assert r.status_code == 200
    rows = r.json()
    assert any(x["student_id"] == demo["id"] for x in rows)

    # Idempotent upsert (mark again should not duplicate)
    requests.post(f"{API}/attendance/mark", json=payload, headers=admin_h)
    r = requests.get(f"{API}/attendance?date={today}", headers=admin_h)
    count = sum(1 for x in r.json() if x["student_id"] == demo["id"] and x["date"] == today)
    assert count == 1, f"Expected 1 row (upsert), got {count}"

    # Student my-stats
    r = requests.get(f"{API}/attendance/my-stats", headers=demo_h)
    assert r.status_code == 200, r.text
    j = r.json()
    assert "percentage" in j and "total" in j
    assert j["total"] >= 1


# ---------------- Study Material ----------------
def test_study_material_admin_create_and_student_visibility(admin_h, demo_h):
    payload = {
        "title": f"TEST_iter12_Notes_{TS}",
        "file_url": "https://example.com/notes.pdf",
        "class_level": "12th",
        "subject": "Math",
        "chapter": "Calculus",
        "type": "pdf",
        "is_published": True,
    }
    r = requests.post(f"{API}/admin/study-material", json=payload, headers=admin_h)
    assert r.status_code == 200, r.text
    m = r.json()
    _created["materials"].append(m["id"])
    assert m["title"] == payload["title"]

    # Also create an unscoped one (empty class_level) to verify $or visibility
    payload2 = dict(payload, title=f"TEST_iter12_UnscopedNotes_{TS}", class_level="")
    r2 = requests.post(f"{API}/admin/study-material", json=payload2, headers=admin_h)
    assert r2.status_code == 200
    m2 = r2.json()
    _created["materials"].append(m2["id"])

    # Student GET
    r = requests.get(f"{API}/student/study-material", headers=demo_h)
    assert r.status_code == 200, r.text
    rows = r.json()
    titles = {x["title"] for x in rows}
    assert payload["title"] in titles
    assert payload2["title"] in titles


# ---------------- Notifications (in-app + MOCKED) ----------------
def test_broadcast_inapp_and_mocked_channels(admin_h, demo_h):
    payload = {
        "title": f"TEST_iter12_Broadcast_{TS}",
        "message": "Hello students!",
        "audience": "all_students",
        "channels": ["in_app", "email", "whatsapp"],
    }
    r = requests.post(f"{API}/admin/notifications/broadcast", json=payload, headers=admin_h)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert j["recipients"] >= 1
    mocked = j.get("channels_mocked") or []
    # No Resend/Twilio keys configured in this env
    assert "email" in mocked
    assert "whatsapp" in mocked

    # History
    r = requests.get(f"{API}/admin/notifications/history", headers=admin_h)
    assert r.status_code == 200
    hist = r.json()
    assert any(h.get("broadcast_id") == j["broadcast_id"] for h in hist)

    # Student inbox
    r = requests.get(f"{API}/student/notifications", headers=demo_h)
    assert r.status_code == 200
    inbox = r.json()
    assert any(n["title"] == payload["title"] for n in inbox)


# ---------------- Exam batch visibility ----------------
def test_exam_batch_visibility_filter(admin_h, demo_h):
    """If exam has batch_ids set, students NOT in those batches must not see it."""
    # Create a fresh batch unrelated to demo
    rb = requests.post(f"{API}/batches", json={"name": f"TEST_iter12_BatchZ_{TS}", "class_level": "12th"}, headers=admin_h)
    assert rb.status_code == 200
    bz = rb.json()
    _created["batches"].append(bz["id"])

    # Create exam targeting only that batch
    payload = {
        "name": f"TEST_iter12_BatchOnlyExam_{TS}",
        "class_level": "12th",
        "duration_minutes": 60,
        "is_published": True,
        "assigned_student_ids": [],
        "batch_ids": [bz["id"]],
    }
    r = requests.post(f"{API}/exams", json=payload, headers=admin_h)
    assert r.status_code == 200, r.text
    e = r.json()
    _created["exams"].append(e["id"])

    # Demo student should NOT see it (not in batch z)
    r = requests.get(f"{API}/exams", headers=demo_h)
    assert r.status_code == 200
    names = {x["name"] for x in r.json()}
    assert payload["name"] not in names, "Exam with foreign batch_ids leaked to demo student"
