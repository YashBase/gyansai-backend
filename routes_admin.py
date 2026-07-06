"""Admin routes: institute settings, students, dashboard stats, courses, test series."""
from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from typing import List, Optional
from core import db, require_admin, hash_password, new_id, now_utc, iso, clean_doc
from models import (
    InstituteSettingsIn, StudentIn, StudentUpdate, CourseIn, TestSeriesIn,
    PaymentDecisionIn,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------- Institute Settings ----------
@router.get("/settings")
async def get_settings(_admin=Depends(require_admin)):
    s = await db.institute_settings.find_one({"id": "default"}, {"_id": 0})
    return s or {}


@router.put("/settings")
async def update_settings(data: InstituteSettingsIn, _admin=Depends(require_admin)):
    update = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    update["updated_at"] = iso(now_utc())
    await db.institute_settings.update_one({"id": "default"}, {"$set": update}, upsert=True)
    s = await db.institute_settings.find_one({"id": "default"}, {"_id": 0})
    return s


# ---------- Dashboard ----------
@router.get("/dashboard")
async def admin_dashboard(_admin=Depends(require_admin)):
    total_students = await db.students.count_documents({})
    active_students = await db.students.count_documents({"status": "active"})
    total_courses = await db.courses.count_documents({})
    total_exams = await db.exams.count_documents({})
    active_exams = await db.exams.count_documents({"is_published": True})
    total_attempts = await db.attempts.count_documents({})

    # Revenue
    payments = await db.payments.find({"status": "success"}, {"_id": 0}).to_list(1000)
    revenue = sum(p.get("amount", 0) for p in payments)

    # Recent activities
    activities = await db.activities.find({}, {"_id": 0}).sort("created_at", -1).limit(10).to_list(10)

    # Live attempts
    live = await db.attempts.find({"status": "in_progress"}, {"_id": 0}).limit(20).to_list(20)

    # Chart data: revenue by day (last 14 days)
    from collections import defaultdict
    rev_by_day = defaultdict(float)
    for p in payments:
        day = (p.get("created_at") or "")[:10]
        if day:
            rev_by_day[day] += p.get("amount", 0)
    revenue_chart = [{"date": k, "amount": v} for k, v in sorted(rev_by_day.items())[-14:]]

    # Student growth: cumulative count by month
    students = await db.students.find({}, {"_id": 0, "created_at": 1}).to_list(10000)
    growth = defaultdict(int)
    for s in students:
        m = (s.get("created_at") or "")[:7]
        if m:
            growth[m] += 1
    student_growth = [{"month": k, "count": v} for k, v in sorted(growth.items())]

    # Exam performance — avg score per exam
 # Exam performance — avg score per exam

# Exam performance — avg score per exam
perf = await db.attempts.aggregate([
    {"$match": {"status": "submitted"}},
    {"$group": {"_id": "$exam_id", "avg_score": {"$avg": "$score"}, "attempts": {"$sum": 1}}},
    {"$limit": 8},
])
exam_performance = []
for p in perf:
    ex = await db.exams.find_one({"id": p["_id"]}, {"_id": 0, "name": 1})
    exam_performance.append({
        "name": ex["name"] if ex else "Exam",
        "avg": round(p.get("avg_score") or 0, 2),
        "attempts": p.get("attempts", 0)
    })
    return {
        "kpis": {
            "total_students": total_students,
            "active_students": active_students,
            "total_courses": total_courses,
            "total_exams": total_exams,
            "active_exams": active_exams,
            "total_attempts": total_attempts,
            "revenue": revenue,
        },
        "revenue_chart": revenue_chart,
        "student_growth": student_growth,
        "score_chart": exam_performance,
        "recent_activities": activities,
        "live_attempts": live,
    }


# ---------- Students ----------
@router.get("/students")
async def list_students(_admin=Depends(require_admin), q: Optional[str] = None, status: Optional[str] = None):
    flt = {}
    if q:
        flt["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"username": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
            {"enrollment_no": {"$regex": q, "$options": "i"}},
        ]
    if status:
        flt["status"] = status
    students = await db.students.find(flt, {"_id": 0, "password_hash": 0}).sort("created_at", -1).to_list(2000)
    return students


@router.post("/students")
async def create_student(data: StudentIn, _admin=Depends(require_admin)):
    existing = await db.students.find_one({"username": data.username})
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    doc = {
        "id": new_id(),
        "name": data.name,
        "username": data.username,
        "password_hash": hash_password(data.password or "student123"),
        "email": data.email or "",
        "mobile": data.mobile or "",
        "parent_mobile": data.parent_mobile or "",
        "school": data.school or "",
        "enrollment_no": data.enrollment_no or "",
        "photo_url": data.photo_url or "",
        "class_level": data.class_level or "",
        "batch_id": data.batch_id or "",
        "signup_status": data.signup_status or "approved",
        "status": "active",
        "course_ids": [],
        "exam_ids": [],
        "created_at": iso(now_utc()),
    }
    await db.students.insert_one(doc)
    clean_doc(doc)
    return doc


@router.put("/students/{student_id}")
async def update_student(student_id: str, data: StudentUpdate, _admin=Depends(require_admin)):
    update = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if "password" in update:
        update["password_hash"] = hash_password(update.pop("password"))
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    res = await db.students.update_one({"id": student_id}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Student not found")
    s = await db.students.find_one({"id": student_id}, {"_id": 0, "password_hash": 0})
    return s


@router.delete("/students/{student_id}")
async def delete_student(student_id: str, _admin=Depends(require_admin)):
    res = await db.students.delete_one({"id": student_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Student not found")
    return {"ok": True}


@router.post("/students/{student_id}/assign")
async def assign_to_student(student_id: str, payload: dict, _admin=Depends(require_admin)):
    """Assign course_ids and/or exam_ids to a student."""
    update = {}
    if "course_ids" in payload:
        update["course_ids"] = payload["course_ids"]
    if "exam_ids" in payload:
        update["exam_ids"] = payload["exam_ids"]
    if not update:
        raise HTTPException(status_code=400, detail="Provide course_ids or exam_ids")
    res = await db.students.update_one({"id": student_id}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Student not found")
    return {"ok": True}


@router.post("/students/bulk-import")
async def bulk_import_students(file: UploadFile = File(...), _admin=Depends(require_admin)):
    content = await file.read()
    try:
        import openpyxl
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"openpyxl is required for bulk import: {exc}")
    try:
        wb = openpyxl.load_workbook(BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid xlsx file: {e}")
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {"imported": 0, "errors": ["Empty file"]}
    headers = [str(h).strip().lower() if h else "" for h in rows[0]]
    required = ["name", "username"]
    for r in required:
        if r not in headers:
            raise HTTPException(status_code=400, detail=f"Missing column: {r}")
    imported = 0
    errors = []
    for i, row in enumerate(rows[1:], start=2):
        data = {headers[j]: (row[j] if j < len(row) else None) for j in range(len(headers))}
        if not data.get("name") or not data.get("username"):
            errors.append(f"Row {i}: name/username missing")
            continue
        if await db.students.find_one({"username": str(data["username"])}):
            errors.append(f"Row {i}: username '{data['username']}' exists")
            continue
        await db.students.insert_one({
            "id": new_id(),
            "name": str(data["name"]),
            "username": str(data["username"]),
            "password_hash": hash_password(str(data.get("password") or "student123")),
            "email": str(data.get("email") or ""),
            "mobile": str(data.get("mobile") or ""),
            "enrollment_no": str(data.get("enrollment_no") or ""),
            "photo_url": "",
            "status": "active",
            "course_ids": [],
            "exam_ids": [],
            "created_at": iso(now_utc()),
        })
        imported += 1
    return {"imported": imported, "errors": errors}


# ---------- Courses ----------
@router.get("/courses")
async def list_courses(_admin=Depends(require_admin)):
    return await db.courses.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)


@router.post("/courses")
async def create_course(data: CourseIn, _admin=Depends(require_admin)):
    chapters = []
    for ch in data.chapters:
        d = ch.model_dump()
        d["id"] = d.get("id") or new_id()
        chapters.append(d)
    doc = data.model_dump()
    doc["chapters"] = chapters
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.courses.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.put("/courses/{course_id}")
async def update_course(course_id: str, data: CourseIn, _admin=Depends(require_admin)):
    update = data.model_dump()
    chapters = []
    for ch in update.get("chapters", []):
        ch["id"] = ch.get("id") or new_id()
        chapters.append(ch)
    update["chapters"] = chapters
    res = await db.courses.update_one({"id": course_id}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Course not found")
    return await db.courses.find_one({"id": course_id}, {"_id": 0})


@router.delete("/courses/{course_id}")
async def delete_course(course_id: str, _admin=Depends(require_admin)):
    res = await db.courses.delete_one({"id": course_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Course not found")
    return {"ok": True}


# ---------- Test Series ----------
@router.get("/test-series")
async def list_test_series(_admin=Depends(require_admin)):
    return await db.test_series.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)


@router.post("/test-series")
async def create_test_series(data: TestSeriesIn, _admin=Depends(require_admin)):
    doc = data.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.test_series.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.put("/test-series/{ts_id}")
async def update_test_series(ts_id: str, data: TestSeriesIn, _admin=Depends(require_admin)):
    res = await db.test_series.update_one({"id": ts_id}, {"$set": data.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Test series not found")
    return await db.test_series.find_one({"id": ts_id}, {"_id": 0})


@router.delete("/test-series/{ts_id}")
async def delete_test_series(ts_id: str, _admin=Depends(require_admin)):
    await db.test_series.delete_one({"id": ts_id})
    return {"ok": True}


# ---------- Payments approval ----------
@router.get("/payments")
async def list_payments(_admin=Depends(require_admin), status: Optional[str] = None):
    flt = {}
    if status:
        flt["status"] = status
    return await db.payments.find(flt, {"_id": 0}).sort("created_at", -1).to_list(2000)


@router.post("/payments/{payment_id}/approve")
async def approve_payment(payment_id: str, payload: Optional[PaymentDecisionIn] = None, _admin=Depends(require_admin)):
    p = await db.payments.find_one({"id": payment_id}, {"_id": 0})
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    if p.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"Payment is already {p.get('status')}")

    # Re-fetch item & grant access
    coll_map = {"course": "courses", "test_series": "test_series", "exam": "exams"}
    coll = coll_map.get(p["item_type"])
    item = await db[coll].find_one({"id": p["item_id"]}, {"_id": 0}) if coll else None
    if not item:
        raise HTTPException(status_code=404, detail="Underlying item no longer exists")

    if p["item_type"] == "course":
        await db.students.update_one({"id": p["user_id"]}, {"$addToSet": {"course_ids": p["item_id"]}})
    elif p["item_type"] == "exam":
        await db.students.update_one({"id": p["user_id"]}, {"$addToSet": {"exam_ids": p["item_id"]}})
    elif p["item_type"] == "test_series":
        ids = item.get("exam_ids") or []
        if ids:
            await db.students.update_one({"id": p["user_id"]}, {"$addToSet": {"exam_ids": {"$each": ids}}})

    update = {
        "status": "success",
        "approved_at": iso(now_utc()),
        "approval_note": (payload.reason if payload else "") or "",
    }
    await db.payments.update_one({"id": payment_id}, {"$set": update})
    await db.activities.insert_one({
        "id": new_id(),
        "type": "payment_approved",
        "text": f"Approved payment of ₹{p.get('amount')} from {p.get('user_name')} for '{p.get('item_name')}' (UTR {p.get('utr')})",
        "created_at": iso(now_utc()),
    })
    return await db.payments.find_one({"id": payment_id}, {"_id": 0})


@router.post("/payments/{payment_id}/reject")
async def reject_payment(payment_id: str, payload: PaymentDecisionIn, _admin=Depends(require_admin)):
    p = await db.payments.find_one({"id": payment_id}, {"_id": 0})
    if not p:
        raise HTTPException(status_code=404, detail="Payment not found")
    if p.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"Payment is already {p.get('status')}")
    await db.payments.update_one({"id": payment_id}, {"$set": {
        "status": "rejected",
        "rejected_at": iso(now_utc()),
        "rejection_reason": payload.reason or "Not specified",
    }})
    await db.activities.insert_one({
        "id": new_id(),
        "type": "payment_rejected",
        "text": f"Rejected payment from {p.get('user_name')} for '{p.get('item_name')}' — {payload.reason or 'no reason'}",
        "created_at": iso(now_utc()),
    })
    return await db.payments.find_one({"id": payment_id}, {"_id": 0})

