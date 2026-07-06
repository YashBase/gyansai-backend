"""Teacher routes — login + admin-managed CRUD. Teachers get a restricted admin role."""
from fastapi import APIRouter, Depends, HTTPException
from core import db, require_admin, hash_password, verify_password, new_id, now_utc, iso, create_token
from models import TeacherIn, TeacherLoginIn, TeacherUpdate

router = APIRouter(tags=["teachers"])


@router.post("/auth/teacher/login")
async def teacher_login(data: TeacherLoginIn):
    t = await db.teachers.find_one({"email": data.email.lower()}, {"_id": 0})
    if not t or not verify_password(data.password, t.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if t.get("status") == "suspended":
        raise HTTPException(status_code=403, detail="Account suspended")
    token = create_token(t["id"], "teacher")
    t.pop("password_hash", None)
    return {"token": token, "user": t, "role": "teacher"}


@router.get("/admin/teachers")
async def list_teachers(_admin=Depends(require_admin)):
    rows = await db.teachers.find({}, {"_id": 0, "password_hash": 0}).sort("created_at", -1).to_list(500)
    return rows


@router.post("/admin/teachers")
async def create_teacher(data: TeacherIn, _admin=Depends(require_admin)):
    existing = await db.teachers.find_one({"email": data.email.lower()})
    if existing:
        raise HTTPException(status_code=409, detail="Email already in use")
    doc = {
        "id": new_id(),
        "name": data.name,
        "email": data.email.lower(),
        "password_hash": hash_password(data.password),
        "mobile": data.mobile or "",
        "subjects": data.subjects or [],
        "status": "active",
        "created_at": iso(now_utc()),
    }
    await db.teachers.insert_one(doc)
    doc.pop("password_hash", None)
    doc.pop("_id", None)
    return doc


@router.put("/admin/teachers/{teacher_id}")
async def update_teacher(teacher_id: str, data: TeacherUpdate, _admin=Depends(require_admin)):
    upd = {k: v for k, v in data.model_dump(exclude_none=True).items() if k != "password"}
    if data.password:
        upd["password_hash"] = hash_password(data.password)
    res = await db.teachers.update_one({"id": teacher_id}, {"$set": upd})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Teacher not found")
    return await db.teachers.find_one({"id": teacher_id}, {"_id": 0, "password_hash": 0})


@router.delete("/admin/teachers/{teacher_id}")
async def delete_teacher(teacher_id: str, _admin=Depends(require_admin)):
    await db.teachers.delete_one({"id": teacher_id})
    return {"ok": True}
