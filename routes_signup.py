"""Public student signup route. Status starts as 'pending' unless settings.auto_approve_signups is true."""
from fastapi import APIRouter, HTTPException
from core import db, new_id, now_utc, iso, hash_password, create_token
from models import StudentSignupIn
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["signup"])


def _username_from_mobile(mobile: str) -> str:
    return ("u" + (mobile or "").strip().lstrip("+").replace(" ", ""))[:20]


@router.post("/signup")
async def student_signup(data: StudentSignupIn):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        settings = await db.get_collection("settings").find_one({}, {"_id": 0}) or {}
    except Exception as exc:
        logger.warning("Signup settings lookup failed: %s", exc)
        settings = {}

    auto = bool(settings.get("auto_approve_signups", False)) or bool(data.from_share_link)

    if data.class_level not in ("11th", "12th"):
        raise HTTPException(status_code=400, detail="class_level must be 11th or 12th")

    requested = (data.username or "").strip().lower()
    import re as _re
    if requested:
        if not _re.fullmatch(r"[a-z0-9_.]{3,24}", requested):
            raise HTTPException(status_code=400, detail="Username must be 3-24 chars: lowercase letters, digits, dot or underscore")

    username = requested or _username_from_mobile(data.mobile)

    try:
        students_col = db.get_collection("students")
        if requested:
            if await students_col.find_one({"username": requested}):
                raise HTTPException(status_code=409, detail="Username already taken — please choose another")
        existing = await students_col.find_one({"$or": [{"mobile": data.mobile}, {"username": username}]})
        if existing:
            raise HTTPException(status_code=409, detail="An account with this mobile/username already exists. Please log in.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Signup duplicate-check failed: %s", exc)
        raise HTTPException(status_code=503, detail="Database query failed") from exc

    doc = {
        "id": new_id(),
        "name": data.name.strip(),
        "username": username,
        "password_hash": hash_password(data.password),
        "email": (data.email or "").strip().lower(),
        "mobile": data.mobile.strip(),
        "parent_mobile": (data.parent_mobile or "").strip(),
        "school": (data.school or "").strip(),
        "class_level": data.class_level,
        "batch_id": data.batch_id or "",
        "enrollment_no": "",
        "photo_url": "",
        "status": "active",
        "signup_status": "approved" if auto else "pending",
        "signup_mode": "auto" if auto else "manual",
        "course_ids": [],
        "exam_ids": [data.target_exam_id] if data.target_exam_id else [],
        "created_at": iso(now_utc()),
    }

    try:
        await students_col.insert_one(doc)
        if data.target_exam_id:
            await db.get_collection("exams").update_one(
                {"id": data.target_exam_id},
                {"$addToSet": {"assigned_student_ids": doc["id"]}},
            )
        await db.get_collection("activities").insert_one({
            "id": new_id(),
            "type": "student_signup",
            "text": f"Signup: {doc['name']} ({doc['mobile']}, {doc['class_level']}) — {doc['signup_status']}",
            "created_at": iso(now_utc()),
        })
    except Exception as exc:
        logger.warning("Signup write failed: %s", exc)
        raise HTTPException(status_code=503, detail="Signup could not be saved") from exc

    doc.pop("_id", None)
    doc.pop("password_hash", None)
    if auto:
        token = create_token(doc["id"], "student")
        return {"token": token, "user": doc, "role": "student", "auto_approved": True}
    return {"ok": True, "auto_approved": False, "message": "Signup successful. Awaiting admin approval before you can log in.", "username": username}


@router.post("/admin/students/{student_id}/approve")
async def approve_student(student_id: str):
    """Lightweight approve — kept under /auth for grouping; require_admin check happens via routes_admin."""
    from core import db as _db
    res = await _db.students.update_one({"id": student_id}, {"$set": {"signup_status": "approved", "status": "active"}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Student not found")
    return {"ok": True}
