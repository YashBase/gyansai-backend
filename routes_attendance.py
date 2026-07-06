"""Attendance routes — daily/batch/student attendance tracking."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from core import db, require_admin, require_student, new_id, now_utc, iso
from models import AttendanceMarkIn

router = APIRouter(prefix="/attendance", tags=["attendance"])


@router.post("/mark")
async def mark_attendance(data: AttendanceMarkIn, _admin=Depends(require_admin)):
    """Upsert one row per (date, student_id)."""
    for e in data.entries:
        sid = e.get("student_id")
        status = e.get("status", "present")
        if not sid:
            continue
        await db.attendance.update_one(
            {"date": data.date, "student_id": sid},
            {
                "$set": {
                    "status": status,
                    "batch_id": data.batch_id or "",
                    "class_level": data.class_level or "",
                    "note": data.note or "",
                    "marked_at": iso(now_utc()),
                },
                "$setOnInsert": {
                    "id": new_id(),
                    "date": data.date,
                    "student_id": sid,
                },
            },
            upsert=True,
        )
    return {"ok": True, "marked": len(data.entries)}


@router.get("")
async def list_attendance(date: Optional[str] = None, batch_id: Optional[str] = None, class_level: Optional[str] = None, _admin=Depends(require_admin)):
    flt = {}
    if date:
        flt["date"] = date
    if batch_id:
        flt["batch_id"] = batch_id
    if class_level:
        flt["class_level"] = class_level
    rows = await db.attendance.find(flt, {"_id": 0}).sort("date", -1).limit(1000).to_list(1000)
    return rows


@router.get("/my-stats")
async def my_attendance(student=Depends(require_student)):
    """Student-facing attendance summary."""
    rows = await db.attendance.find({"student_id": student["id"]}, {"_id": 0}).to_list(1000)
    total = len(rows)
    present = sum(1 for r in rows if r.get("status") == "present")
    late = sum(1 for r in rows if r.get("status") == "late")
    pct = round((present + late * 0.5) / total * 100, 1) if total else 0
    return {
        "total": total,
        "present": present,
        "late": late,
        "absent": total - present - late,
        "percentage": pct,
        "recent": sorted(rows, key=lambda x: x.get("date", ""), reverse=True)[:30],
    }
