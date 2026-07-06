"""Iter9 — Test Folder field + Quick-Assign Exam wizard backend tests.

Covers:
- POST /api/questions with test_folder persists
- GET /api/questions?test_folder=... filters
- GET /api/questions/meta returns test_folders array
- POST /api/questions/quick-assign-exam happy path + edge cases
- Class-level visibility filter on student-side GET /api/exams
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
ADMIN_EMAIL = "admin@gyansai.com"
ADMIN_PASS = "admin123"
DEMO_USER = "demo"
DEMO_PASS = "demo123"

FOLDER = f"TEST_iter9_{int(time.time())}"
EXAM_NAME = f"TEST_iter9_Auto_Exam_{int(time.time())}"
NEW_STUDENT_USERNAME = f"TEST_iter9_s11_{int(time.time())}"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/admin/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def demo_token():
    r = requests.post(f"{BASE_URL}/api/auth/student/login",
                      json={"username": DEMO_USER, "password": DEMO_PASS})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def demo_headers(demo_token):
    return {"Authorization": f"Bearer {demo_token}"}


@pytest.fixture(scope="module")
def created_question_ids(admin_headers):
    """Create 3 questions tagged with FOLDER."""
    ids = []
    for i in range(3):
        payload = {
            "title": f"TEST_iter9 Q{i+1}",
            "subject": "Mathematics",
            "test_folder": FOLDER,
            "type": "mcq_single",
            "options": [{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
            "correct_answer": "A",
            "marks": 4.0,
            "negative_marks": 1.0,
        }
        r = requests.post(f"{BASE_URL}/api/questions", json=payload, headers=admin_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["test_folder"] == FOLDER
        ids.append(body["id"])
    return ids


# ------- Question test_folder field tests -------
class TestQuestionFolderField:
    def test_question_persists_test_folder(self, created_question_ids, admin_headers):
        # GET back the first question via list filter
        r = requests.get(f"{BASE_URL}/api/questions",
                         params={"test_folder": FOLDER}, headers=admin_headers)
        assert r.status_code == 200
        items = r.json()
        ids_in_resp = {q["id"] for q in items}
        for qid in created_question_ids:
            assert qid in ids_in_resp
            # Each must have correct folder
        folders = {q.get("test_folder") for q in items}
        assert FOLDER in folders

    def test_meta_returns_test_folders(self, created_question_ids, admin_headers):
        r = requests.get(f"{BASE_URL}/api/questions/meta", headers=admin_headers)
        assert r.status_code == 200
        body = r.json()
        assert "test_folders" in body
        assert isinstance(body["test_folders"], list)
        assert FOLDER in body["test_folders"]
        # Should not contain empty strings
        assert "" not in body["test_folders"]


# ------- Quick-Assign Exam tests -------
class TestQuickAssignExam:
    created_exam_id = None
    new_student_id = None

    def test_400_when_test_folder_empty(self, admin_headers):
        r = requests.post(
            f"{BASE_URL}/api/questions/quick-assign-exam",
            json={"test_folder": "", "exam_name": "x", "class_level": "12th"},
            headers=admin_headers,
        )
        assert r.status_code == 400, r.text

    def test_400_when_exam_name_empty(self, admin_headers):
        r = requests.post(
            f"{BASE_URL}/api/questions/quick-assign-exam",
            json={"test_folder": FOLDER, "exam_name": "", "class_level": "12th"},
            headers=admin_headers,
        )
        assert r.status_code == 400, r.text

    def test_404_when_folder_has_no_questions(self, admin_headers):
        r = requests.post(
            f"{BASE_URL}/api/questions/quick-assign-exam",
            json={"test_folder": "TEST_iter9_nonexistent_folder_xyz",
                  "exam_name": "x", "class_level": "12th"},
            headers=admin_headers,
        )
        assert r.status_code == 404, r.text
        assert "No questions found" in r.text

    def test_happy_path_creates_exam_and_assigns(self, admin_headers, created_question_ids):
        payload = {
            "test_folder": FOLDER,
            "exam_name": EXAM_NAME,
            "exam_tag": "JEE Mains",
            "class_level": "12th",
            "duration_minutes": 90,
            "is_published": True,
            "auto_assign_class_students": True,
        }
        r = requests.post(f"{BASE_URL}/api/questions/quick-assign-exam",
                          json=payload, headers=admin_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "exam" in body
        assert body["questions_count"] == len(created_question_ids)
        assert body["assigned_count"] >= 1  # demo has class_level=12th
        exam = body["exam"]
        assert exam["name"] == EXAM_NAME
        assert exam["class_level"] == "12th"
        assert exam["exam_tag"] == "JEE Mains"
        assert exam["is_published"] is True
        assert set(exam["question_ids"]) == set(created_question_ids)
        assert exam["test_folder_source"] == FOLDER
        TestQuickAssignExam.created_exam_id = exam["id"]

        # Verify via GET /api/exams (admin)
        r2 = requests.get(f"{BASE_URL}/api/exams", headers=admin_headers)
        assert r2.status_code == 200
        exam_ids = {e["id"] for e in r2.json()}
        assert exam["id"] in exam_ids

    def test_demo_student_sees_assigned_exam(self, demo_headers):
        """Demo is class_level=12th and should now see the new exam."""
        assert TestQuickAssignExam.created_exam_id, "previous test must run first"
        r = requests.get(f"{BASE_URL}/api/exams", headers=demo_headers)
        assert r.status_code == 200, r.text
        exam_ids = {e["id"] for e in r.json()}
        assert TestQuickAssignExam.created_exam_id in exam_ids, \
            f"demo (12th) should see exam {TestQuickAssignExam.created_exam_id}"

    def test_class_level_filter_excludes_other_class(self, admin_headers):
        """Create a TEST 11th student → confirm they do NOT see the 12th-only exam."""
        # 1. Create an 11th student
        s_payload = {
            "name": "TEST_iter9 11th student",
            "username": NEW_STUDENT_USERNAME,
            "password": "test1234",
            "class_level": "11th",
        }
        r = requests.post(f"{BASE_URL}/api/admin/students",
                          json=s_payload, headers=admin_headers)
        assert r.status_code == 200, r.text
        TestQuickAssignExam.new_student_id = r.json()["id"]

        # 2. Login as them
        r = requests.post(f"{BASE_URL}/api/auth/student/login",
                          json={"username": NEW_STUDENT_USERNAME, "password": "test1234"})
        assert r.status_code == 200, r.text
        s_token = r.json()["token"]
        s_headers = {"Authorization": f"Bearer {s_token}"}

        # 3. Confirm they do NOT see the 12th-only auto-assigned exam
        r = requests.get(f"{BASE_URL}/api/exams", headers=s_headers)
        assert r.status_code == 200
        exam_ids = {e["id"] for e in r.json()}
        assert TestQuickAssignExam.created_exam_id not in exam_ids, \
            "11th-student must NOT see exam auto-assigned to 12th only"


# ------- Cleanup -------
@pytest.fixture(scope="module", autouse=True)
def cleanup(admin_headers, created_question_ids):
    yield
    # Delete created questions
    for qid in created_question_ids:
        try:
            requests.delete(f"{BASE_URL}/api/questions/{qid}", headers=admin_headers)
        except Exception:
            pass
    # Delete created exam
    if TestQuickAssignExam.created_exam_id:
        try:
            requests.delete(f"{BASE_URL}/api/exams/{TestQuickAssignExam.created_exam_id}",
                            headers=admin_headers)
        except Exception:
            pass
    # Delete created student
    if TestQuickAssignExam.new_student_id:
        try:
            requests.delete(f"{BASE_URL}/api/admin/students/{TestQuickAssignExam.new_student_id}",
                            headers=admin_headers)
        except Exception:
            pass
