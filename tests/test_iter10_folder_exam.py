"""Iter10 — Exam Folder Manager (create/update/delete via /api/questions/folder-exam)."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://iit-test-portal.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"
TS = int(time.time())
FOLDER = f"TEST_iter10_folder_{TS}"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/admin/login", json={"email": "admin@gyansai.com", "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def created_questions(headers):
    ids = []
    for i in range(3):
        payload = {
            "title": f"TEST_iter10 question {i} {TS}",
            "subject": "Mathematics",
            "difficulty": "medium",
            "type": "mcq_single",
            "options": [{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
            "correct_answer": "A",
            "marks": 4,
            "negative_marks": 1,
        }
        r = requests.post(f"{API}/questions", json=payload, headers=headers)
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    yield ids
    # Cleanup
    for qid in ids:
        requests.delete(f"{API}/questions/{qid}", headers=headers)


@pytest.fixture(scope="module")
def demo_student_id(headers):
    r = requests.get(f"{API}/admin/students", headers=headers)
    assert r.status_code == 200
    for s in r.json():
        if s.get("username") == "demo":
            return s["id"]
    pytest.skip("demo student not found")


# State across tests
state = {}


class TestFolderExamFlow:
    def test_create_folder_exam(self, headers, created_questions, demo_student_id):
        payload = {
            "folder_name": FOLDER,
            "exam_name": f"TEST_iter10 Exam {TS}",
            "exam_tag": "JEE Mains",
            "class_level": "12th",
            "duration_minutes": 90,
            "question_ids": created_questions,
            "assigned_student_ids": [demo_student_id],
            "auto_assign_class_students": False,
            "tag_questions_to_folder": True,
            "is_published": True,
        }
        r = requests.post(f"{API}/questions/folder-exam", json=payload, headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["action"] == "created"
        assert data["questions_count"] == 3
        assert data["assigned_count"] == 1
        assert "exam" in data and data["exam"]["id"]
        assert data["exam"]["test_folder_source"] == FOLDER
        assert data["exam"]["class_level"] == "12th"
        state["exam_id"] = data["exam"]["id"]

    def test_questions_tagged_with_folder(self, headers, created_questions):
        r = requests.get(f"{API}/questions", params={"test_folder": FOLDER}, headers=headers)
        assert r.status_code == 200
        rows = r.json()
        ids = {q["id"] for q in rows}
        for qid in created_questions:
            assert qid in ids, f"Question {qid} missing test_folder tag"
        for q in rows:
            assert q["test_folder"] == FOLDER

    def test_folders_listing_includes_new(self, headers):
        r = requests.get(f"{API}/questions/folders", headers=headers)
        assert r.status_code == 200
        match = [f for f in r.json() if f["folder_name"] == FOLDER]
        assert len(match) == 1, "Folder missing from /folders"
        row = match[0]
        assert row["question_count"] == 3
        assert row["exam_id"] == state["exam_id"]
        assert row["class_level"] == "12th"
        assert row["exam_tag"] == "JEE Mains"
        assert row["assigned_count"] == 1
        assert row["is_published"] is True

    def test_demo_student_sees_exam(self):
        # Login as demo
        r = requests.post(f"{API}/auth/student/login", json={"username": "demo", "password": "demo123"})
        assert r.status_code == 200, r.text
        stoken = r.json()["token"]
        r2 = requests.get(f"{API}/exams", headers={"Authorization": f"Bearer {stoken}"})
        assert r2.status_code == 200
        exam_ids = {e["id"] for e in r2.json()}
        assert state["exam_id"] in exam_ids, "Demo student doesn't see assigned exam"

    def test_update_folder_exam(self, headers, created_questions):
        # Update — remove demo, keep no students, change exam_name
        payload = {
            "folder_name": FOLDER,
            "exam_id": state["exam_id"],
            "exam_name": f"TEST_iter10 Exam UPDATED {TS}",
            "exam_tag": "JEE Advanced",
            "class_level": "12th",
            "duration_minutes": 120,
            "question_ids": created_questions[:2],  # drop one
            "assigned_student_ids": [],  # remove demo
            "auto_assign_class_students": False,
            "tag_questions_to_folder": True,
            "is_published": True,
        }
        r = requests.post(f"{API}/questions/folder-exam", json=payload, headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["action"] == "updated"
        assert data["exam"]["id"] == state["exam_id"], "exam_id changed on update"
        assert data["exam"]["name"].endswith("UPDATED " + str(TS))
        assert data["exam"]["duration_minutes"] == 120
        assert data["assigned_count"] == 0

        # Verify demo's exam_ids no longer contains this exam (visibility via /api/exams may still
        # surface via class_level filter — that's a separate design concern; spec says pull from exam_ids).
        r2 = requests.get(f"{API}/admin/students", headers=headers)
        demo_doc = next((s for s in r2.json() if s.get("username") == "demo"), None)
        assert demo_doc is not None
        assert state["exam_id"] not in (demo_doc.get("exam_ids") or []), "exam_id should be pulled from demo.exam_ids"

    def test_auto_assign_class_students(self, headers, created_questions):
        # Switch to auto-assign 12th; demo should reappear
        payload = {
            "folder_name": FOLDER,
            "exam_id": state["exam_id"],
            "exam_name": f"TEST_iter10 Exam AUTO {TS}",
            "exam_tag": "JEE Mains",
            "class_level": "12th",
            "duration_minutes": 60,
            "question_ids": created_questions,
            "assigned_student_ids": [],
            "auto_assign_class_students": True,
            "tag_questions_to_folder": True,
            "is_published": True,
        }
        r = requests.post(f"{API}/questions/folder-exam", json=payload, headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["assigned_count"] >= 1, "Auto-assign by class should pull at least demo (12th)"

    def test_edge_empty_folder_name(self, headers):
        r = requests.post(f"{API}/questions/folder-exam", json={"folder_name": "  ", "exam_name": "X"}, headers=headers)
        assert r.status_code == 400

    def test_edge_empty_exam_name(self, headers):
        r = requests.post(f"{API}/questions/folder-exam", json={"folder_name": "X", "exam_name": ""}, headers=headers)
        assert r.status_code == 400

    def test_delete_folder(self, headers, created_questions):
        r = requests.delete(f"{API}/questions/folders/{FOLDER}", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["exams_deleted"] >= 1
        # Verify questions still exist but no folder tag
        r2 = requests.get(f"{API}/questions", params={"test_folder": FOLDER}, headers=headers)
        assert len(r2.json()) == 0
        for qid in created_questions:
            rq = requests.get(f"{API}/questions", params={"q": "TEST_iter10"}, headers=headers)
            ids = {q["id"] for q in rq.json()}
            assert qid in ids, "Question deleted when only folder tag should be dropped"
            break
        # Verify exam deleted
        r3 = requests.get(f"{API}/exams/{state['exam_id']}", headers=headers)
        assert r3.status_code == 404, "Exam should be deleted"


class TestRegression:
    def test_quick_assign_btn_endpoint(self, headers, created_questions):
        """Regression: legacy POST /api/questions/quick-assign-exam still works."""
        folder = f"TEST_iter10_qa_{TS}"
        # Re-tag questions into this folder first
        for qid in created_questions:
            r = requests.put(f"{API}/questions/{qid}", json={
                "title": "TEST_iter10 qa", "subject": "Mathematics",
                "test_folder": folder, "difficulty": "medium", "type": "mcq_single",
                "options": [{"key": "A", "text": "1"}], "correct_answer": "A",
                "marks": 4, "negative_marks": 1,
            }, headers=headers)
            assert r.status_code == 200
        payload = {
            "test_folder": folder,
            "exam_name": f"TEST_iter10 QA {TS}",
            "class_level": "12th",
            "auto_assign_class_students": True,
        }
        r = requests.post(f"{API}/questions/quick-assign-exam", json=payload, headers=headers)
        assert r.status_code == 200
        eid = r.json()["exam"]["id"]
        # Cleanup
        requests.delete(f"{API}/questions/folders/{folder}", headers=headers)

    def test_meta_endpoint(self, headers):
        r = requests.get(f"{API}/questions/meta", headers=headers)
        assert r.status_code == 200
        data = r.json()
        for key in ("subjects", "chapters", "topics", "test_folders", "total"):
            assert key in data
