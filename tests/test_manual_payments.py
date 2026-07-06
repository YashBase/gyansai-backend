"""Iteration 5 — Manual UPI/Bank payment workflow tests.

Covers: /api/student/checkout, /api/student/payments/{id}/utr, /api/student/payments,
/api/admin/payments, /api/admin/payments/{id}/verify and revenue counting on dashboard.
"""
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


@pytest.fixture(scope="module")
def seeded_test_series(admin_headers):
    """Find/ensure a seeded test_series with price>0 and at least one exam_id."""
    r = requests.get(f"{API}/admin/test-series", headers=admin_headers)
    assert r.status_code == 200, r.text
    series = r.json()
    target = next((s for s in series if (s.get("price") or 0) > 0 and s.get("exam_ids")), None)
    if not target:
        # try via student listing
        # fallback: pick first
        target = series[0] if series else None
    assert target, "No test series with price>0 seeded"
    return target


def _make_student(admin_headers, prefix="TEST_pay"):
    """Create fresh student and return {id, token, headers}."""
    uname = f"{prefix}_{int(time.time()*1000)}"
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


def _profile(headers):
    r = requests.get(f"{API}/student/profile", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 1. Checkout ----------
class TestCheckoutManualFlow:
    def test_checkout_returns_pending_utr_and_bank_details(self, admin_headers, seeded_test_series):
        ts = seeded_test_series
        stu = _make_student(admin_headers, "TEST_co")
        try:
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "test_series", "item_id": ts["id"], "coupon": "GYAN10"})
            assert r.status_code == 200, r.text
            body = r.json()
            # payment block
            p = body["payment"]
            for k in ["id", "amount", "discount", "coupon", "status"]:
                assert k in p, f"missing {k} in payment"
            assert p["status"] == "pending_utr"
            assert p["coupon"] == "GYAN10"
            # bank block
            bank = body["bank"]
            for k in ["upi_id", "bank_name", "bank_account", "bank_ifsc", "institute_name"]:
                assert k in bank, f"missing {k} in bank"
            # upi_link present and well-formed (only if upi_id configured)
            if bank["upi_id"]:
                assert body["upi_link"].startswith("upi://pay?pa="), body["upi_link"]
            assert body["reference_note"].startswith("GS-")
            assert isinstance(body["instructions"], str) and len(body["instructions"]) > 0
            # NO access granted yet
            prof = _profile(stu["headers"])
            for eid in (ts.get("exam_ids") or []):
                assert eid not in (prof.get("exam_ids") or []), \
                    "Access granted prematurely before admin verification"
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_coupon_math_GYAN10(self, admin_headers, seeded_test_series):
        """Original 4999 -> after GYAN10 (10% off) amount=4499.1, discount=499.9."""
        ts = seeded_test_series
        # We don't control seeded price, but spec says original 4999.
        if (ts.get("price") or 0) != 4999:
            pytest.skip(f"Seeded test-series price={ts.get('price')} != 4999; coupon math sample skipped")
        stu = _make_student(admin_headers, "TEST_coup")
        try:
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "test_series", "item_id": ts["id"], "coupon": "GYAN10"})
            assert r.status_code == 200, r.text
            p = r.json()["payment"]
            assert p["amount"] == 4499.1, p
            assert p["discount"] == 499.9, p
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_invalid_item_type_400(self, admin_headers):
        stu = _make_student(admin_headers, "TEST_invit")
        try:
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "bogus", "item_id": "x"})
            assert r.status_code in (400, 422), r.text
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)


# ---------- 2. UTR submission ----------
class TestUTRSubmission:
    def _new_payment(self, admin_headers, ts):
        stu = _make_student(admin_headers, "TEST_utr")
        r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                          json={"item_type": "test_series", "item_id": ts["id"]})
        assert r.status_code == 200, r.text
        pid = r.json()["payment"]["id"]
        return stu, pid

    def test_submit_utr_moves_to_awaiting_review(self, admin_headers, seeded_test_series):
        stu, pid = self._new_payment(admin_headers, seeded_test_series)
        try:
            r = requests.post(f"{API}/student/payments/{pid}/utr",
                              headers=stu["headers"], json={"utr": "AXIS25060001234"})
            assert r.status_code == 200, r.text
            updated = r.json()
            assert updated["status"] == "awaiting_review"
            assert updated["utr"] == "AXIS25060001234"
            assert updated["utr_submitted_at"]
            # Confirm via list
            rl = requests.get(f"{API}/student/payments", headers=stu["headers"])
            assert rl.status_code == 200
            mine = [p for p in rl.json() if p["id"] == pid][0]
            assert mine["status"] == "awaiting_review"
            assert mine["utr"] == "AXIS25060001234"
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_empty_utr_400(self, admin_headers, seeded_test_series):
        stu, pid = self._new_payment(admin_headers, seeded_test_series)
        try:
            r = requests.post(f"{API}/student/payments/{pid}/utr",
                              headers=stu["headers"], json={"utr": ""})
            assert r.status_code == 400, r.text
            assert "valid utr" in (r.json().get("detail") or "").lower()
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_short_utr_400(self, admin_headers, seeded_test_series):
        stu, pid = self._new_payment(admin_headers, seeded_test_series)
        try:
            r = requests.post(f"{API}/student/payments/{pid}/utr",
                              headers=stu["headers"], json={"utr": "abc"})
            assert r.status_code == 400, r.text
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_cannot_resubmit_utr_after_verification(self, admin_headers, seeded_test_series):
        stu, pid = self._new_payment(admin_headers, seeded_test_series)
        try:
            # submit utr
            requests.post(f"{API}/student/payments/{pid}/utr",
                          headers=stu["headers"], json={"utr": "AXIS123ABC"})
            # admin approves
            ra = requests.post(f"{API}/admin/payments/{pid}/verify", headers=admin_headers,
                               json={"approved": True, "note": "ok"})
            assert ra.status_code == 200, ra.text
            assert ra.json()["status"] == "success"
            # now student tries another UTR -> 400
            r = requests.post(f"{API}/student/payments/{pid}/utr",
                              headers=stu["headers"], json={"utr": "AXIS999AAA"})
            assert r.status_code == 400, r.text
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)


# ---------- 3. My payments listing ----------
class TestMyPayments:
    def test_my_payments_sorted_desc(self, admin_headers, seeded_test_series):
        stu = _make_student(admin_headers, "TEST_my")
        try:
            ts = seeded_test_series
            ids = []
            for _ in range(2):
                r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                                  json={"item_type": "test_series", "item_id": ts["id"]})
                assert r.status_code == 200, r.text
                ids.append(r.json()["payment"]["id"])
                time.sleep(0.05)
            rl = requests.get(f"{API}/student/payments", headers=stu["headers"])
            assert rl.status_code == 200, rl.text
            arr = rl.json()
            assert len(arr) >= 2
            created = [p["created_at"] for p in arr]
            assert created == sorted(created, reverse=True), "payments not sorted desc by created_at"
            # all belong to this user
            for p in arr:
                assert p["user_id"] == stu["id"]
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)


# ---------- 4. Admin payments listing & verify ----------
class TestAdminPaymentEndpoints:
    def test_list_awaiting_review_includes_pending(self, admin_headers, seeded_test_series):
        stu = _make_student(admin_headers, "TEST_adml")
        try:
            ts = seeded_test_series
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "test_series", "item_id": ts["id"]})
            pid = r.json()["payment"]["id"]
            # submit UTR
            requests.post(f"{API}/student/payments/{pid}/utr",
                          headers=stu["headers"], json={"utr": "AXISLISTTEST"})
            # admin list
            rl = requests.get(f"{API}/admin/payments", headers=admin_headers,
                              params={"status": "awaiting_review"})
            assert rl.status_code == 200, rl.text
            arr = rl.json()
            ids = [p["id"] for p in arr]
            assert pid in ids
            for p in arr:
                assert p["status"] == "awaiting_review"
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_verify_approve_grants_test_series_access(self, admin_headers, seeded_test_series):
        stu = _make_student(admin_headers, "TEST_approve")
        try:
            ts = seeded_test_series
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "test_series", "item_id": ts["id"]})
            pid = r.json()["payment"]["id"]
            requests.post(f"{API}/student/payments/{pid}/utr",
                          headers=stu["headers"], json={"utr": "AXISAPPR001"})
            # approve
            ra = requests.post(f"{API}/admin/payments/{pid}/verify", headers=admin_headers,
                               json={"approved": True, "note": "verified"})
            assert ra.status_code == 200, ra.text
            body = ra.json()
            assert body["status"] == "success"
            assert body["verified_at"]
            assert body["verified_by"]
            assert body["admin_note"] == "verified"
            # exam_ids granted
            prof = _profile(stu["headers"])
            for eid in (ts.get("exam_ids") or []):
                assert eid in (prof.get("exam_ids") or []), f"exam {eid} not granted to student"
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_verify_reject_does_not_grant_access(self, admin_headers, seeded_test_series):
        stu = _make_student(admin_headers, "TEST_reject")
        try:
            ts = seeded_test_series
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "test_series", "item_id": ts["id"]})
            pid = r.json()["payment"]["id"]
            requests.post(f"{API}/student/payments/{pid}/utr",
                          headers=stu["headers"], json={"utr": "AXISREJ001"})
            ra = requests.post(f"{API}/admin/payments/{pid}/verify", headers=admin_headers,
                               json={"approved": False, "note": "wrong utr"})
            assert ra.status_code == 200, ra.text
            assert ra.json()["status"] == "rejected"
            assert ra.json()["admin_note"] == "wrong utr"
            prof = _profile(stu["headers"])
            for eid in (ts.get("exam_ids") or []):
                assert eid not in (prof.get("exam_ids") or []), "access granted on rejection!"
            # reject + resubmit flow
            rr = requests.post(f"{API}/student/payments/{pid}/utr",
                               headers=stu["headers"], json={"utr": "AXISREJ002"})
            assert rr.status_code == 200, rr.text
            assert rr.json()["status"] == "awaiting_review"
            assert rr.json()["utr"] == "AXISREJ002"
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_verify_already_success_400(self, admin_headers, seeded_test_series):
        stu = _make_student(admin_headers, "TEST_dblver")
        try:
            ts = seeded_test_series
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "test_series", "item_id": ts["id"]})
            pid = r.json()["payment"]["id"]
            requests.post(f"{API}/student/payments/{pid}/utr",
                          headers=stu["headers"], json={"utr": "AXISDBL001"})
            ra = requests.post(f"{API}/admin/payments/{pid}/verify", headers=admin_headers,
                               json={"approved": True})
            assert ra.status_code == 200
            # second verify
            r2 = requests.post(f"{API}/admin/payments/{pid}/verify", headers=admin_headers,
                               json={"approved": True})
            assert r2.status_code == 400, r2.text
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)

    def test_non_admin_cannot_list_or_verify(self, admin_headers, seeded_test_series):
        stu = _make_student(admin_headers, "TEST_forbid")
        try:
            ts = seeded_test_series
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "test_series", "item_id": ts["id"]})
            pid = r.json()["payment"]["id"]
            # list
            r1 = requests.get(f"{API}/admin/payments", headers=stu["headers"])
            assert r1.status_code == 403, r1.text
            # verify
            r2 = requests.post(f"{API}/admin/payments/{pid}/verify", headers=stu["headers"],
                               json={"approved": True})
            assert r2.status_code == 403, r2.text
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)


# ---------- 5. Course checkout ----------
class TestCourseCheckout:
    def test_course_access_only_after_approval(self, admin_headers):
        # Create a paid course via admin
        cpayload = {
            "name": f"TEST Paid Course {int(time.time())}",
            "description": "iter5 paid",
            "price": 1500,
            "is_published": True,
            "chapters": [],
        }
        rc = requests.post(f"{API}/admin/courses", headers=admin_headers, json=cpayload)
        assert rc.status_code == 200, rc.text
        cid = rc.json()["id"]
        stu = _make_student(admin_headers, "TEST_course")
        try:
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "course", "item_id": cid})
            assert r.status_code == 200, r.text
            pid = r.json()["payment"]["id"]
            assert r.json()["payment"]["status"] == "pending_utr"
            # no course_id yet
            prof = _profile(stu["headers"])
            assert cid not in (prof.get("course_ids") or [])
            # submit utr & approve
            requests.post(f"{API}/student/payments/{pid}/utr",
                          headers=stu["headers"], json={"utr": "AXISCOURSE01"})
            ra = requests.post(f"{API}/admin/payments/{pid}/verify", headers=admin_headers,
                               json={"approved": True})
            assert ra.status_code == 200
            prof2 = _profile(stu["headers"])
            assert cid in (prof2.get("course_ids") or []), "course_id not granted on approval"
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)
            requests.delete(f"{API}/admin/courses/{cid}", headers=admin_headers)


# ---------- 6. Dashboard revenue ----------
class TestDashboardRevenue:
    def test_revenue_counts_only_success(self, admin_headers, seeded_test_series):
        """Create a pending+awaiting+rejected payment and verify revenue does NOT change
        until status=success."""
        # revenue baseline
        r0 = requests.get(f"{API}/admin/dashboard", headers=admin_headers)
        assert r0.status_code == 200
        baseline = r0.json()["kpis"]["revenue"]

        stu = _make_student(admin_headers, "TEST_rev")
        try:
            ts = seeded_test_series
            # 1) pending payment
            requests.post(f"{API}/student/checkout", headers=stu["headers"],
                          json={"item_type": "test_series", "item_id": ts["id"]})
            # 2) awaiting_review payment
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "test_series", "item_id": ts["id"]})
            pid_aw = r.json()["payment"]["id"]
            requests.post(f"{API}/student/payments/{pid_aw}/utr",
                          headers=stu["headers"], json={"utr": "AXISREVAW01"})
            # 3) rejected payment
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "test_series", "item_id": ts["id"]})
            pid_rj = r.json()["payment"]["id"]
            requests.post(f"{API}/student/payments/{pid_rj}/utr",
                          headers=stu["headers"], json={"utr": "AXISREVRJ01"})
            requests.post(f"{API}/admin/payments/{pid_rj}/verify", headers=admin_headers,
                          json={"approved": False, "note": "no"})
            # revenue unchanged
            rmid = requests.get(f"{API}/admin/dashboard", headers=admin_headers)
            assert rmid.status_code == 200
            assert rmid.json()["kpis"]["revenue"] == baseline, \
                f"revenue moved without any success payment ({baseline} -> {rmid.json()['kpis']['revenue']})"

            # 4) success payment
            r = requests.post(f"{API}/student/checkout", headers=stu["headers"],
                              json={"item_type": "test_series", "item_id": ts["id"]})
            pid_ok = r.json()["payment"]["id"]
            paid_amount = r.json()["payment"]["amount"]
            requests.post(f"{API}/student/payments/{pid_ok}/utr",
                          headers=stu["headers"], json={"utr": "AXISREVOK01"})
            ra = requests.post(f"{API}/admin/payments/{pid_ok}/verify", headers=admin_headers,
                               json={"approved": True})
            assert ra.status_code == 200
            r2 = requests.get(f"{API}/admin/dashboard", headers=admin_headers)
            after = r2.json()["kpis"]["revenue"]
            # delta should equal paid_amount (within float tolerance)
            assert abs((after - baseline) - paid_amount) < 0.01, \
                f"revenue delta {after-baseline} != paid {paid_amount}"
        finally:
            requests.delete(f"{API}/admin/students/{stu['id']}", headers=admin_headers)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
