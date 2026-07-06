"""Iteration 7: Exam folders grouping + PDF/Photo OCR upload."""
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://iit-test-portal.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@gyansai.com"
ADMIN_PASS = "admin123"
STUDENT_USER = "demo"
STUDENT_PASS = "demo123"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{API}/auth/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def student_token():
    # Student login uses username
    r = requests.post(f"{API}/auth/student/login", json={"username": STUDENT_USER, "password": STUDENT_PASS}, timeout=15)
    if r.status_code != 200:
        # alt payload
        r = requests.post(f"{API}/auth/student/login", json={"email": STUDENT_USER, "password": STUDENT_PASS}, timeout=15)
    assert r.status_code == 200, f"student login failed: {r.status_code} {r.text}"
    return r.json()["token"]


def H(t):
    return {"Authorization": f"Bearer {t}"}


# ---------- Auth ----------
class TestAuth:
    def test_admin_login(self, admin_token):
        assert isinstance(admin_token, str) and len(admin_token) > 10

    def test_student_login(self, student_token):
        assert isinstance(student_token, str) and len(student_token) > 10


# ---------- Exam Folder Grouping ----------
class TestExamFolders:
    def test_admin_lists_exams_with_exam_tag_field(self, admin_token):
        r = requests.get(f"{API}/exams", headers=H(admin_token), timeout=15)
        assert r.status_code == 200
        exams = r.json()
        assert isinstance(exams, list)
        # Required test exams should exist
        names = {e.get("name"): e for e in exams}
        assert "JEE Mock 1" in names, f"JEE Mock 1 missing. names={list(names.keys())}"
        assert "NEET Mock 1" in names, f"NEET Mock 1 missing. names={list(names.keys())}"
        assert names["JEE Mock 1"].get("exam_tag") == "JEE"
        assert names["NEET Mock 1"].get("exam_tag") == "NEET"
        # Older exams may not have exam_tag set; UI handles missing/empty as "Uncategorized"

    def test_admin_create_exam_with_tag(self, admin_token):
        payload = {
            "name": "TEST_FolderExam_iter7",
            "description": "iter7 folder",
            "type": "mock",
            "duration_minutes": 30,
            "exam_tag": "TEST_TAG",
            "passing_marks": 0,
            "instructions": "n/a",
            "randomize": False,
            "negative_marking": True,
            "question_ids": [],
            "allowed_tab_switches": 3,
            "enable_webcam": False,
            "is_published": False,
            "price": 0,
        }
        r = requests.post(f"{API}/exams", json=payload, headers=H(admin_token), timeout=15)
        assert r.status_code == 200, r.text
        eid = r.json()["id"]
        assert r.json().get("exam_tag") == "TEST_TAG"
        # Verify it appears in list with same tag
        r2 = requests.get(f"{API}/exams", headers=H(admin_token), timeout=15)
        found = [e for e in r2.json() if e["id"] == eid]
        assert found and found[0]["exam_tag"] == "TEST_TAG"
        # cleanup
        requests.delete(f"{API}/exams/{eid}", headers=H(admin_token), timeout=15)

    def test_student_sees_published_exams_with_tags(self, student_token):
        r = requests.get(f"{API}/exams", headers=H(student_token), timeout=15)
        assert r.status_code == 200
        exams = r.json()
        assert isinstance(exams, list)
        names = {e.get("name"): e for e in exams}
        # Student should see both published free exams
        assert "JEE Mock 1" in names, f"Student missing JEE Mock 1. Got: {list(names.keys())}"
        assert "NEET Mock 1" in names, f"Student missing NEET Mock 1. Got: {list(names.keys())}"
        assert names["JEE Mock 1"].get("exam_tag") == "JEE"
        assert names["NEET Mock 1"].get("exam_tag") == "NEET"
        # exam_tag may be missing on older docs; UI groups those under Uncategorized.


# ---------- OCR PDF Upload ----------
class TestOcrPdfUpload:
    def test_ocr_upload_requires_auth(self):
        with open("/tmp/test_qs.pdf", "rb") as f:
            r = requests.post(f"{API}/questions/ocr/upload", files={"file": ("test_qs.pdf", f, "application/pdf")}, timeout=30)
        assert r.status_code in (401, 403)

    def test_ocr_upload_pdf(self, admin_token):
        with open("/tmp/test_qs.pdf", "rb") as f:
            r = requests.post(
                f"{API}/questions/ocr/upload",
                files={"file": ("test_qs.pdf", f, "application/pdf")},
                headers=H(admin_token),
                timeout=120,
            )
        assert r.status_code == 200, f"OCR upload failed: {r.status_code} {r.text[:500]}"
        data = r.json()
        # Shape checks
        assert "questions" in data
        assert "pages_processed" in data
        assert "total_pages" in data
        assert isinstance(data["questions"], list)
        assert data["total_pages"] >= 1
        assert data["pages_processed"] >= 1
        # The test PDF has 1 page with content "Q1. What is 2+2? A) 3 B) 4"
        # OCR may or may not return a question depending on Vision parsing; we just ensure no crash
        # If questions returned, each must have title
        for q in data["questions"]:
            assert "title" in q
