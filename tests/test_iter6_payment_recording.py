"""Iteration 6 — Manual UPI payment-request flow + AV recording chunk upload/playback.

Covers:
  /api/student/payment-request (pending/auto-approve/duplicate-UTR/empty-UTR)
  /api/admin/payments (list, approve, reject) — 403 for non-admin
  /api/student/my-payments (status visibility)
  /api/exams/recording-chunk (upload + 413 cap)
  /api/exams/admin/attempts/{aid}/recording (list metadata, no payload)
  /api/exams/admin/attempts/{aid}/recording/{cid} (binary stream)
  /api/public/recording-chunks/{aid} (in_progress -> 404, submitted -> list, no auth)
  /api/public/recording-chunk/{aid}/{cid} (binary stream, no auth)
"""
import base64
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@gyansai.com"
ADMIN_PASS = "admin123"


# ---------- Fixtures ----------
@pytest.fixture(scope="module")
def admin_headers():
    r = requests.post(f"{API}/auth/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=30)
    assert r.status_code == 200, f"admin login failed: {r.text}"
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _make_student(admin_headers, prefix="TEST_pay"):
    """Create a fresh student and return {id, username, token, headers}."""
    uname = f"{prefix}_{int(time.time() * 1000)}"
    pw = "pass1234"
    r = requests.post(f"{API}/admin/students", headers=admin_headers,
                      json={"name": "TEST Pay", "username": uname, "password": pw})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    rl = requests.post(f"{API}/auth/student/login", json={"username": uname, "password": pw})
    assert rl.status_code == 200, rl.text
    token = rl.json()["token"]
    return {"id": sid, "username": uname, "token": token,
            "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture(scope="module")
def paid_course(admin_headers):
    """Find a seeded paid course; if none, create one (auto-cleaned)."""
    r = requests.get(f"{API}/admin/courses", headers=admin_headers)
    assert r.status_code == 200, r.text
    courses = r.json()
    target = next((c for c in courses if (c.get("price") or 0) > 0), None)
    created = None
    if not target:
        cp = {"name": f"TEST Paid Course {int(time.time())}", "description": "iter6",
              "price": 2999, "is_published": True, "chapters": []}
        rc = requests.post(f"{API}/admin/courses", headers=admin_headers, json=cp)
        assert rc.status_code == 200, rc.text
        target = rc.json()
        created = target["id"]
    yield target
    if created:
        requests.delete(f"{API}/admin/courses/{created}", headers=admin_headers)


@pytest.fixture(scope="module")
def free_course(admin_headers):
    """Create a free course just for testing auto-approve path."""
    cp = {"name": f"TEST Free Course {int(time.time())}", "description": "iter6 free",
          "price": 0, "is_published": True, "chapters": []}
    rc = requests.post(f"{API}/admin/courses", headers=admin_headers, json=cp)
    assert rc.status_code == 200, rc.text
    cid = rc.json()["id"]
    yield rc.json()
    requests.delete(f"{API}/admin/courses/{cid}", headers=admin_headers)


@pytest.fixture(scope="module")
def published_exam_with_questions(admin_headers):
    """Find a published exam that has question_ids — used for fresh attempts."""
    r = requests.get(f"{API}/exams", headers=admin_headers)
    assert r.status_code == 200
    target = next((e for e in r.json() if e.get("question_ids") and e.get("is_published")), None)
    assert target, "No published exam with questions seeded"
    return target


def _profile(headers):
    r = requests.get(f"{API}/student/profile", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ====================================================================
# 1. /api/student/payment-request
# ====================================================================
class TestPaymentRequest:
    def test_paid_course_creates_pending(self, admin_headers, paid_course):
        stu = _make_student(admin_headers, "TEST_pay_pending")
        utr = f"TESTUTR{int(time.time() * 1000)}"
        try:
            r = requests.post(f"{API}/student/payment-request", headers=stu["headers"],
                              json={"item_type": "course", "item_id": paid_course["id"], "utr": utr})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["auto_approved"] is False
            p = body["payment"]
            assert p["status"] == "pending"
            assert p["item_type"] == "course"
            assert p["item_id"] == paid_course["id"]
            assert p["utr"] == utr
            assert p["amount"] == float(paid_course["price"])
            # NO access yet
            prof = _profile(stu["headers"])
            assert paid_course["id"] not in (prof.get("course_ids") or []), \
                "Access granted before approval!"
            # Visible in my-payments as pending
            rmp = requests.get(f"{API}/student/my-payments", headers=stu["headers"])
            assert rmp.status_code == 200
            mine = [x for x in rmp.json() if x["id"] == p["id"]]
            assert mine and mine[0]["status"] == "pending"
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_duplicate_utr_blocked(self, admin_headers, paid_course):
        utr = f"DUPUTR{int(time.time() * 1000)}"
        stu1 = _make_student(admin_headers, "TEST_dup_a")
        stu2 = _make_student(admin_headers, "TEST_dup_b")
        try:
            r1 = requests.post(f"{API}/student/payment-request", headers=stu1["headers"],
                               json={"item_type": "course", "item_id": paid_course["id"], "utr": utr})
            assert r1.status_code == 200, r1.text
            # second student tries same UTR
            r2 = requests.post(f"{API}/student/payment-request", headers=stu2["headers"],
                               json={"item_type": "course", "item_id": paid_course["id"], "utr": utr})
            assert r2.status_code == 400, r2.text
            assert "already" in (r2.json().get("detail") or "").lower()
        finally:
            requests.delete(f"{API}/admin/students/{stu1['id']}", headers=admin_headers)
            requests.delete(f"{API}/admin/students/{stu2['id']}", headers=admin_headers)

    def test_empty_utr_for_paid_400(self, admin_headers, paid_course):
        stu = _make_student(admin_headers, "TEST_empty")
        try:
            r = requests.post(f"{API}/student/payment-request", headers=stu["headers"],
                              json={"item_type": "course", "item_id": paid_course["id"], "utr": ""})
            assert r.status_code == 400, r.text
            assert "utr" in (r.json().get("detail") or "").lower()
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_free_item_auto_approves(self, admin_headers, free_course):
        stu = _make_student(admin_headers, "TEST_free")
        try:
            # NOTE: PaymentRequestIn model requires `utr` field; for free items the route
            # short-circuits before UTR validation, so an empty string is accepted.
            r = requests.post(f"{API}/student/payment-request", headers=stu["headers"],
                              json={"item_type": "course", "item_id": free_course["id"], "utr": ""})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["auto_approved"] is True
            p = body["payment"]
            assert p["status"] == "success"
            assert p["amount"] == 0
            # Access granted immediately
            prof = _profile(stu["headers"])
            assert free_course["id"] in (prof.get("course_ids") or []), \
                "Free item did not grant access automatically"
            # GET /api/student/courses should now show it
            rc = requests.get(f"{API}/student/courses", headers=stu["headers"])
            assert rc.status_code == 200
            ids = [c["id"] for c in rc.json()]
            assert free_course["id"] in ids
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)


# ====================================================================
# 2. Admin payments list + 403
# ====================================================================
class TestAdminPaymentsList:
    def test_admin_pending_list(self, admin_headers, paid_course):
        stu = _make_student(admin_headers, "TEST_alist")
        utr = f"LIST{int(time.time() * 1000)}"
        try:
            r = requests.post(f"{API}/student/payment-request", headers=stu["headers"],
                              json={"item_type": "course", "item_id": paid_course["id"], "utr": utr})
            pid = r.json()["payment"]["id"]
            rl = requests.get(f"{API}/admin/payments", headers=admin_headers,
                              params={"status": "pending"})
            assert rl.status_code == 200, rl.text
            ids = [p["id"] for p in rl.json()]
            assert pid in ids
            for p in rl.json():
                assert p["status"] == "pending"
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_non_admin_forbidden(self, admin_headers):
        stu = _make_student(admin_headers, "TEST_forbid")
        try:
            r = requests.get(f"{API}/admin/payments", headers=stu["headers"])
            assert r.status_code == 403, r.text
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)


# ====================================================================
# 3. Approve flow
# ====================================================================
class TestApproveFlow:
    def test_approve_grants_course_access(self, admin_headers, paid_course):
        stu = _make_student(admin_headers, "TEST_appr")
        utr = f"APPR{int(time.time() * 1000)}"
        try:
            r = requests.post(f"{API}/student/payment-request", headers=stu["headers"],
                              json={"item_type": "course", "item_id": paid_course["id"], "utr": utr})
            pid = r.json()["payment"]["id"]
            ra = requests.post(f"{API}/admin/payments/{pid}/approve", headers=admin_headers,
                               json={"reason": "verified"})
            assert ra.status_code == 200, ra.text
            updated = ra.json()
            assert updated["status"] == "success"
            assert updated.get("approved_at")
            # Profile: course_ids gained
            prof = _profile(stu["headers"])
            assert paid_course["id"] in (prof.get("course_ids") or [])
            # GET /api/student/courses returns it
            rc = requests.get(f"{API}/student/courses", headers=stu["headers"])
            assert rc.status_code == 200
            assert paid_course["id"] in [c["id"] for c in rc.json()]
            # /api/student/my-payments shows status=success
            rmp = requests.get(f"{API}/student/my-payments", headers=stu["headers"])
            row = next(x for x in rmp.json() if x["id"] == pid)
            assert row["status"] == "success"
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)


# ====================================================================
# 4. Reject flow
# ====================================================================
class TestRejectFlow:
    def test_reject_no_access_and_cannot_reapprove(self, admin_headers, paid_course):
        stu = _make_student(admin_headers, "TEST_rej")
        utr = f"REJ{int(time.time() * 1000)}"
        try:
            r = requests.post(f"{API}/student/payment-request", headers=stu["headers"],
                              json={"item_type": "course", "item_id": paid_course["id"], "utr": utr})
            pid = r.json()["payment"]["id"]
            rr = requests.post(f"{API}/admin/payments/{pid}/reject", headers=admin_headers,
                               json={"reason": "UTR mismatch"})
            assert rr.status_code == 200, rr.text
            body = rr.json()
            assert body["status"] == "rejected"
            assert body["rejection_reason"] == "UTR mismatch"
            # Student did NOT gain access
            prof = _profile(stu["headers"])
            assert paid_course["id"] not in (prof.get("course_ids") or [])
            # Re-approving a rejected payment -> 400
            ra2 = requests.post(f"{API}/admin/payments/{pid}/approve", headers=admin_headers,
                                json={"reason": "oops"})
            assert ra2.status_code == 400, ra2.text
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)


# ====================================================================
# 5+6. Recording chunk upload + 413 cap
# ====================================================================
@pytest.fixture(scope="module")
def fresh_attempt(admin_headers, published_exam_with_questions):
    """Create fresh student + attempt for recording-chunk tests."""
    eid = published_exam_with_questions["id"]
    stu = _make_student(admin_headers, "TEST_rec")
    requests.post(f"{API}/admin/students/{stu['id']}/assign", headers=admin_headers,
                  json={"exam_ids": [eid]})
    ra = requests.post(f"{API}/exams/start", headers=stu["headers"], json={"exam_id": eid})
    assert ra.status_code == 200, ra.text
    aid = ra.json()["id"]
    yield {"student": stu, "exam_id": eid, "attempt_id": aid}
    requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)


class TestRecordingChunkUpload:
    def test_upload_chunk_ok(self, fresh_attempt):
        s = fresh_attempt
        b64 = base64.b64encode(b"fake webm data" * 100).decode()
        r = requests.post(f"{API}/exams/recording-chunk", headers=s["student"]["headers"],
                          json={"attempt_id": s["attempt_id"], "data_base64": b64,
                                "mime_type": "video/webm", "duration_ms": 30000, "chunk_index": 0})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "id" in body and isinstance(body["id"], str)
        assert body["size_bytes"] == len(b64)
        assert body.get("at")
        TestRecordingChunkUpload.chunk_id_0 = body["id"]
        # upload a second chunk for ordering test
        r2 = requests.post(f"{API}/exams/recording-chunk", headers=s["student"]["headers"],
                           json={"attempt_id": s["attempt_id"], "data_base64": b64,
                                 "mime_type": "video/webm", "duration_ms": 30000, "chunk_index": 1})
        assert r2.status_code == 200
        TestRecordingChunkUpload.chunk_id_1 = r2.json()["id"]

    def test_oversized_chunk_413(self, fresh_attempt):
        s = fresh_attempt
        oversized = "A" * 2_000_001
        r = requests.post(f"{API}/exams/recording-chunk", headers=s["student"]["headers"],
                          json={"attempt_id": s["attempt_id"], "data_base64": oversized,
                                "mime_type": "video/webm", "duration_ms": 30000, "chunk_index": 99})
        assert r.status_code == 413, r.text


# ====================================================================
# 7. Admin recording listing (metadata only, no data_base64)
# ====================================================================
class TestAdminRecordingListing:
    def test_admin_lists_chunks_sorted_no_payload(self, admin_headers, fresh_attempt):
        s = fresh_attempt
        r = requests.get(f"{API}/exams/admin/attempts/{s['attempt_id']}/recording",
                         headers=admin_headers)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert isinstance(rows, list)
        assert len(rows) >= 2, "expected at least 2 chunks from upload tests"
        # No data_base64 field
        for row in rows:
            assert "data_base64" not in row, "data_base64 must be excluded from listing"
            assert "id" in row
            assert "at" in row
            assert "size_bytes" in row
        # Sorted ascending by 'at'
        ats = [r["at"] for r in rows]
        assert ats == sorted(ats), "chunks not sorted ascending by 'at'"


# ====================================================================
# 8. Admin chunk binary stream
# ====================================================================
class TestAdminChunkBinary:
    def test_admin_chunk_binary_stream(self, admin_headers, fresh_attempt):
        s = fresh_attempt
        cid = TestRecordingChunkUpload.chunk_id_0
        r = requests.get(f"{API}/exams/admin/attempts/{s['attempt_id']}/recording/{cid}",
                         headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.headers.get("content-type", "").startswith("video/webm")
        # Should decode the base64 back to bytes
        expected = b"fake webm data" * 100
        assert r.content == expected, "chunk bytes mismatch"


# ====================================================================
# 9. Public chunk listing (no auth, submitted only)
# ====================================================================
class TestPublicChunkListing:
    def test_in_progress_attempt_404(self, fresh_attempt):
        s = fresh_attempt
        # attempt is still in_progress
        r = requests.get(f"{API}/public/recording-chunks/{s['attempt_id']}")
        assert r.status_code == 404, r.text

    def test_unknown_id_404(self):
        r = requests.get(f"{API}/public/recording-chunks/does-not-exist-iter6")
        assert r.status_code == 404

    def test_submitted_attempt_returns_list(self, admin_headers, fresh_attempt):
        s = fresh_attempt
        # Submit the attempt now
        rs = requests.post(f"{API}/exams/submit", headers=s["student"]["headers"],
                           json={"attempt_id": s["attempt_id"]})
        assert rs.status_code == 200, rs.text
        # Public list — no auth
        r = requests.get(f"{API}/public/recording-chunks/{s['attempt_id']}")
        assert r.status_code == 200, r.text
        rows = r.json()
        assert isinstance(rows, list)
        assert len(rows) >= 2
        for row in rows:
            assert "data_base64" not in row, "data_base64 must NOT be exposed in public list"
            assert "id" in row and "at" in row


# ====================================================================
# 10. Public chunk binary stream (no auth)
# ====================================================================
class TestPublicChunkBinary:
    def test_public_chunk_binary_no_auth(self, fresh_attempt):
        s = fresh_attempt
        cid = TestRecordingChunkUpload.chunk_id_0
        r = requests.get(f"{API}/public/recording-chunk/{s['attempt_id']}/{cid}")
        assert r.status_code == 200, r.text
        assert r.headers.get("content-type", "").startswith("video/webm")
        expected = b"fake webm data" * 100
        assert r.content == expected

    def test_public_chunk_unknown_chunk_404(self, fresh_attempt):
        s = fresh_attempt
        r = requests.get(f"{API}/public/recording-chunk/{s['attempt_id']}/does-not-exist")
        assert r.status_code == 404

    def test_public_chunk_unknown_attempt_404(self):
        r = requests.get(f"{API}/public/recording-chunk/no-such-attempt/no-such-chunk")
        assert r.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
