"""Student-facing routes: dashboard, my results, courses, test series storefront."""
from fastapi import APIRouter, Depends, HTTPException
from core import db, require_student, get_current_user, new_id, now_utc, iso
from models import CheckoutIn, PaymentRequestIn

router = APIRouter(prefix="/student", tags=["student"])


@router.get("/dashboard")
async def student_dashboard(student=Depends(require_student)):
    sid = student["id"]
    student_doc = await db.students.find_one({"id": sid}, {"_id": 0, "password_hash": 0})

    attempts = await db.attempts.find({"student_id": sid}, {"_id": 0}).sort("started_at", -1).to_list(200)
    submitted = [a for a in attempts if a.get("status") == "submitted"]
    avg_score = round(sum(a.get("score", 0) for a in submitted) / max(len(submitted), 1), 2) if submitted else 0
    total_correct = sum(a.get("correct", 0) for a in submitted)
    total_wrong = sum(a.get("wrong", 0) for a in submitted)
    total_attempted = total_correct + total_wrong
    accuracy = round((total_correct / total_attempted) * 100, 2) if total_attempted else 0

    # Available exams
    avail = await db.exams.find(
        {"$or": [
            {"is_published": True, "price": 0},
            {"id": {"$in": student_doc.get("exam_ids") or []}},
        ]}, {"_id": 0, "question_ids": 0}
    ).sort("created_at", -1).limit(10).to_list(10)
    for e in avail:
        attempt = await db.attempts.find_one(
            {"exam_id": e["id"], "student_id": sid, "status": "submitted"}, {"_id": 0}
        )
        e["attempted"] = bool(attempt)
        e["last_score"] = attempt["score"] if attempt else None
        e["attempt_id"] = attempt["id"] if attempt else None

    # Courses (catalog — all published, plus owned)
    owned_course_ids = student_doc.get("course_ids") or []
    courses = await db.courses.find(
        {"$or": [
            {"is_published": True},
            {"id": {"$in": owned_course_ids}},
        ]}, {"_id": 0}
    ).limit(8).to_list(8)
    for c in courses:
        c["purchased"] = c["id"] in owned_course_ids

    return {
        "student": student_doc,
        "kpis": {
            "exams_taken": len(submitted),
            "avg_score": avg_score,
            "accuracy": accuracy,
            "total_correct": total_correct,
        },
        "recent_attempts": submitted[:5],
        "available_exams": avail,
        "courses": courses,
    }


@router.get("/my-attempts")
async def my_attempts(student=Depends(require_student)):
    return await db.attempts.find({"student_id": student["id"]}, {"_id": 0}).sort("started_at", -1).to_list(500)


@router.get("/courses")
async def my_courses(student=Depends(require_student)):
    """List ALL published courses (free + paid) for the catalog.
    Each row carries `purchased=true` if the student already owns it."""
    student_doc = await db.students.find_one({"id": student["id"]}, {"_id": 0})
    owned = set(student_doc.get("course_ids") or [])
    courses = await db.courses.find(
        {"$or": [
            {"is_published": True},
            {"id": {"$in": list(owned)}},
        ]}, {"_id": 0}
    ).to_list(1000)
    for c in courses:
        c["purchased"] = c["id"] in owned
    return courses


@router.get("/courses/{course_id}")
async def get_course(course_id: str, student=Depends(require_student)):
    c = await db.courses.find_one({"id": course_id}, {"_id": 0})
    if not c:
        raise HTTPException(status_code=404, detail="Course not found")
    student_doc = await db.students.find_one({"id": student["id"]}, {"_id": 0})
    owned = course_id in (student_doc.get("course_ids") or [])
    c["purchased"] = owned
    if not owned and float(c.get("price") or 0) > 0:
        # Paid course not yet owned → return preview only, strip chapter content
        c["chapters"] = []
        c["locked"] = True
    return c


@router.get("/test-series")
async def list_test_series_public(_=Depends(get_current_user)):
    return await db.test_series.find({"is_published": True}, {"_id": 0}).to_list(1000)


@router.post("/checkout")
async def checkout(data: CheckoutIn, user=Depends(get_current_user)):
    """LEGACY mocked checkout — kept for back-compat. Use /payment-request for UTR-based flow."""
    coll_map = {"course": "courses", "test_series": "test_series", "exam": "exams"}
    coll = coll_map.get(data.item_type)
    if not coll:
        raise HTTPException(status_code=400, detail="Invalid item_type")
    item = await db[coll].find_one({"id": data.item_id}, {"_id": 0})
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    amount = item.get("price", 0)
    if data.coupon and data.coupon.upper() == "GYAN10":
        amount = round(amount * 0.9, 2)
    payment = {
        "id": new_id(),
        "user_id": user["id"],
        "user_name": user.get("name"),
        "item_type": data.item_type,
        "item_id": data.item_id,
        "item_name": item.get("name"),
        "amount": amount,
        "status": "success",
        "mock": True,
        "created_at": iso(now_utc()),
    }
    await db.payments.insert_one(payment)
    if user["role"] == "student":
        update = {}
        if data.item_type == "course":
            update["$addToSet"] = {"course_ids": data.item_id}
        elif data.item_type == "exam":
            update["$addToSet"] = {"exam_ids": data.item_id}
        elif data.item_type == "test_series":
            update["$addToSet"] = {"exam_ids": {"$each": item.get("exam_ids") or []}}
        if update:
            await db.students.update_one({"id": user["id"]}, update)
    payment.pop("_id", None)
    return {"payment": payment, "mocked": True}


# ---------- Manual UTR Payment Request ----------
@router.post("/payment-request")
async def create_payment_request(data: PaymentRequestIn, student=Depends(require_student)):
    """Student submits UTR after paying via UPI/bank. Admin approves within 1 hour."""
    coll_map = {"course": "courses", "test_series": "test_series", "exam": "exams"}
    coll = coll_map.get(data.item_type)
    if not coll:
        raise HTTPException(status_code=400, detail="Invalid item_type")
    item = await db[coll].find_one({"id": data.item_id}, {"_id": 0})
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    amount = float(item.get("price") or 0)
    if data.coupon and data.coupon.upper() == "GYAN10":
        amount = round(amount * 0.9, 2)

    # Free items — auto-approve immediately
    if amount == 0:
        payment = {
            "id": new_id(),
            "user_id": student["id"],
            "user_name": student.get("name"),
            "item_type": data.item_type,
            "item_id": data.item_id,
            "item_name": item.get("name"),
            "amount": 0,
            "utr": "",
            "coupon": data.coupon or "",
            "status": "success",
            "approved_at": iso(now_utc()),
            "mock": False,
            "created_at": iso(now_utc()),
        }
        await db.payments.insert_one(payment)
        await _grant_access(student["id"], data.item_type, data.item_id, item)
        payment.pop("_id", None)
        return {"payment": payment, "auto_approved": True}

    if not data.utr or len(data.utr.strip()) < 6:
        raise HTTPException(status_code=400, detail="A valid 12-digit UTR / transaction reference is required")

    # Duplicate UTR guard — same UTR can't be reused for another pending/success payment
    existing = await db.payments.find_one({"utr": data.utr.strip(), "status": {"$in": ["pending", "success"]}})
    if existing:
        raise HTTPException(status_code=400, detail="This UTR has already been submitted")

    payment = {
        "id": new_id(),
        "user_id": student["id"],
        "user_name": student.get("name"),
        "user_username": student.get("username"),
        "item_type": data.item_type,
        "item_id": data.item_id,
        "item_name": item.get("name"),
        "amount": amount,
        "utr": data.utr.strip(),
        "coupon": data.coupon or "",
        "payer_name": data.payer_name or student.get("name"),
        "note": data.note or "",
        "status": "pending",
        "created_at": iso(now_utc()),
    }
    await db.payments.insert_one(payment)
    await db.activities.insert_one({
        "id": new_id(),
        "type": "payment_pending",
        "text": f"{student.get('name')} submitted UTR {data.utr.strip()} for '{item.get('name')}' — ₹{amount}",
        "created_at": iso(now_utc()),
    })
    payment.pop("_id", None)
    return {"payment": payment, "auto_approved": False}


async def _grant_access(student_id: str, item_type: str, item_id: str, item: dict) -> None:
    if item_type == "course":
        await db.students.update_one({"id": student_id}, {"$addToSet": {"course_ids": item_id}})
    elif item_type == "exam":
        await db.students.update_one({"id": student_id}, {"$addToSet": {"exam_ids": item_id}})
    elif item_type == "test_series":
        exam_ids = item.get("exam_ids") or []
        if exam_ids:
            await db.students.update_one({"id": student_id}, {"$addToSet": {"exam_ids": {"$each": exam_ids}}})


@router.get("/my-payments")
async def my_payments(student=Depends(require_student)):
    return await db.payments.find(
        {"user_id": student["id"]}, {"_id": 0},
    ).sort("created_at", -1).to_list(500)


@router.get("/my-purchases")
async def my_purchases(student=Depends(require_student)):
    """Resolved view of paid items the student owns (success status only)."""
    paid = await db.payments.find(
        {"user_id": student["id"], "status": "success"}, {"_id": 0},
    ).sort("created_at", -1).to_list(500)
    return paid


@router.get("/profile")
async def get_profile(student=Depends(require_student)):
    return await db.students.find_one({"id": student["id"]}, {"_id": 0, "password_hash": 0})


@router.put("/profile")
async def update_profile(payload: dict, student=Depends(require_student)):
    allowed = {"name", "email", "mobile", "photo_url"}
    upd = {k: v for k, v in payload.items() if k in allowed}
    if upd:
        await db.students.update_one({"id": student["id"]}, {"$set": upd})
    return await db.students.find_one({"id": student["id"]}, {"_id": 0, "password_hash": 0})
