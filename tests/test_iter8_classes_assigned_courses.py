"""Iteration 8 — Backend tests for:
- class_level on students & exams
- assigned_student_ids picker → student.exam_ids sync
- visibility filter (assigned-only exam hidden from non-assigned student)
- student GET /api/student/courses lists all published (free+paid) with purchased flag
- paid course locked=true + chapters=[] when not purchased
- end-to-end course purchase flow: payment-request → admin approve → access granted
"""
import os
import time
import pytest
import requests

def _load_base_url():
    u = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
    if u:
        return u
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip().rstrip("/")
    except Exception:
        pass
    return ""


BASE_URL = _load_base_url()
assert BASE_URL, "REACT_APP_BACKEND_URL must be configured"
API = f"{BASE_URL}/api"


# ---- Fixtures ----
@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/admin/login",
                      json={"email": "admin@gyansai.com", "password": "admin123"})
    if r.status_code != 200:
        # try alternate login path
        r = requests.post(f"{API}/admin/login",
                          json={"email": "admin@gyansai.com", "password": "admin123"})
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def student_token():
    r = requests.post(f"{API}/auth/student/login",
                      json={"username": "demo", "password": "demo123"})
    if r.status_code != 200:
        r = requests.post(f"{API}/student/login",
                          json={"username": "demo", "password": "demo123"})
    assert r.status_code == 200, f"student login failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_h(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def stu_h(student_token):
    return {"Authorization": f"Bearer {student_token}"}


@pytest.fixture(scope="module")
def demo_student(admin_h):
    r = requests.get(f"{API}/admin/students", headers=admin_h)
    assert r.status_code == 200
    demo = next((s for s in r.json() if s.get("username") == "demo"), None)
    assert demo, "demo student must exist"
    return demo


# ---- Class level on students ----
class TestStudentClassLevel:
    def test_create_student_with_class_level(self, admin_h):
        payload = {"name": "TEST_iter8_stu", "username": "TEST_iter8_stu",
                   "password": "pw12345", "class_level": "12th"}
        r = requests.post(f"{API}/admin/students", json=payload, headers=admin_h)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["class_level"] == "12th"
        # verify persistence via GET
        g = requests.get(f"{API}/admin/students", headers=admin_h)
        match = next((s for s in g.json() if s["id"] == data["id"]), None)
        assert match and match["class_level"] == "12th"
        # cleanup
        requests.delete(f"{API}/admin/students/{data['id']}", headers=admin_h)

    def test_update_student_class_level(self, admin_h):
        c = requests.post(f"{API}/admin/students",
                          json={"name": "TEST_iter8_upd", "username": "TEST_iter8_upd",
                                "password": "pw12345", "class_level": "11th"},
                          headers=admin_h)
        sid = c.json()["id"]
        u = requests.put(f"{API}/admin/students/{sid}",
                         json={"class_level": "12th"}, headers=admin_h)
        assert u.status_code == 200
        assert u.json()["class_level"] == "12th"
        requests.delete(f"{API}/admin/students/{sid}", headers=admin_h)


# ---- Exam class_level + assigned_student_ids ----
class TestExamAssignmentAndClass:
    def test_create_exam_with_class_and_assigned(self, admin_h, demo_student):
        payload = {
            "name": "TEST_iter8_assigned_to_demo",
            "exam_tag": "JEE Mains",
            "class_level": "12th",
            "is_published": True,
            "price": 0,
            "assigned_student_ids": [demo_student["id"]],
            "question_ids": [],
        }
        r = requests.post(f"{API}/exams", json=payload, headers=admin_h)
        assert r.status_code == 200, r.text
        exam = r.json()
        assert exam["class_level"] == "12th"
        assert demo_student["id"] in exam["assigned_student_ids"]
        # verify student.exam_ids now contains the exam id
        s = requests.get(f"{API}/admin/students", headers=admin_h).json()
        demo = next(x for x in s if x["id"] == demo_student["id"])
        assert exam["id"] in (demo.get("exam_ids") or []), \
            "exam id must be added to assigned student's exam_ids"
        # cleanup
        requests.delete(f"{API}/exams/{exam['id']}", headers=admin_h)

    def test_assigned_exam_visible_to_demo(self, admin_h, stu_h, demo_student):
        # exam assigned to demo
        e1 = requests.post(f"{API}/exams", json={
            "name": "TEST_iter8_visible_demo", "is_published": True, "price": 0,
            "assigned_student_ids": [demo_student["id"]], "question_ids": [],
        }, headers=admin_h).json()
        # exam assigned to ONE OTHER student → hidden from demo
        other = requests.post(f"{API}/admin/students", json={
            "name": "TEST_iter8_other", "username": "TEST_iter8_other",
            "password": "pw12345", "class_level": "11th",
        }, headers=admin_h).json()
        e2 = requests.post(f"{API}/exams", json={
            "name": "TEST_iter8_hidden_demo", "is_published": True, "price": 0,
            "assigned_student_ids": [other["id"]], "question_ids": [],
        }, headers=admin_h).json()

        stud_exams = requests.get(f"{API}/exams", headers=stu_h).json()
        names = [e["name"] for e in stud_exams]
        assert "TEST_iter8_visible_demo" in names, "assigned exam must be visible"
        assert "TEST_iter8_hidden_demo" not in names, \
            "exam assigned to other student must NOT be visible to demo"

        # cleanup
        requests.delete(f"{API}/exams/{e1['id']}", headers=admin_h)
        requests.delete(f"{API}/exams/{e2['id']}", headers=admin_h)
        requests.delete(f"{API}/admin/students/{other['id']}", headers=admin_h)


# ---- Student courses catalog & lock ----
class TestStudentCoursesCatalog:
    def test_courses_listing_includes_paid_unpurchased_with_flag(self, admin_h, stu_h):
        # create a paid published course
        c = requests.post(f"{API}/admin/courses", json={
            "name": "TEST_iter8_paid_course", "price": 999, "is_published": True,
            "chapters": [{"title": "Ch1", "videos": [{"title": "v", "url": "u"}],
                          "notes": [], "assignments": []}],
        }, headers=admin_h).json()
        cid = c["id"]
        try:
            stu_courses = requests.get(f"{API}/student/courses", headers=stu_h).json()
            match = next((x for x in stu_courses if x["id"] == cid), None)
            assert match, "paid published course must appear in student catalog"
            assert match["purchased"] is False
            assert float(match.get("price") or 0) == 999

            # paid + not purchased → GET /courses/{id} returns locked=true, chapters=[]
            det = requests.get(f"{API}/student/courses/{cid}", headers=stu_h).json()
            assert det.get("locked") is True
            assert det.get("chapters") == []
            assert det.get("purchased") is False
        finally:
            requests.delete(f"{API}/admin/courses/{cid}", headers=admin_h)

    def test_purchase_flow_end_to_end(self, admin_h, stu_h):
        # create paid course
        course = requests.post(f"{API}/admin/courses", json={
            "name": "TEST_iter8_e2e_course", "price": 500, "is_published": True,
            "chapters": [{"title": "Ch1-secret", "videos": [{"title": "v", "url": "u"}],
                          "notes": [], "assignments": []}],
        }, headers=admin_h).json()
        cid = course["id"]
        try:
            # student creates payment-request
            unique_utr = f"TEST{int(time.time()*1000)}IT8"
            pr = requests.post(f"{API}/student/payment-request", json={
                "item_type": "course", "item_id": cid, "utr": unique_utr,
                "payer_name": "demo", "note": "iter8 test",
            }, headers=stu_h)
            assert pr.status_code == 200, pr.text
            pay = pr.json()
            assert pay.get("auto_approved") is False
            payment_id = pay["payment"]["id"]
            assert pay["payment"]["status"] == "pending"

            # my-payments shows pending
            mp = requests.get(f"{API}/student/my-payments", headers=stu_h).json()
            assert any(p["id"] == payment_id and p["status"] == "pending" for p in mp)

            # admin approves
            ap = requests.post(f"{API}/admin/payments/{payment_id}/approve",
                               json={"reason": "OK"}, headers=admin_h)
            assert ap.status_code == 200, ap.text
            assert ap.json()["status"] == "success"

            # student now sees course as purchased and unlocked
            det = requests.get(f"{API}/student/courses/{cid}", headers=stu_h).json()
            assert det.get("purchased") is True
            assert not det.get("locked"), "locked must be falsy after approval"
            assert len(det.get("chapters") or []) >= 1
            assert det["chapters"][0]["title"] == "Ch1-secret"

            # my-purchases reflects success
            purch = requests.get(f"{API}/student/my-purchases", headers=stu_h).json()
            assert any(p["id"] == payment_id and p["status"] == "success" for p in purch)
        finally:
            # remove course from demo student & delete
            requests.delete(f"{API}/admin/courses/{cid}", headers=admin_h)
