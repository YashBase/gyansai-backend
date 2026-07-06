"""Notification routes — in-app broadcast + MOCKED email/SMS/WhatsApp."""
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from core import db, require_admin, require_student, new_id, now_utc, iso
from models import NotificationBroadcastIn

router = APIRouter(tags=["notifications"])


def _channels_mocked():
    """Return which delivery channels lack real credentials (so we MOCK them)."""
    return {
        "email": not (os.environ.get("RESEND_API_KEY") or os.environ.get("SENDGRID_API_KEY")),
        "sms": not (os.environ.get("TWILIO_AUTH_TOKEN") and os.environ.get("TWILIO_PHONE_NUMBER")),
        "whatsapp": not (os.environ.get("TWILIO_AUTH_TOKEN") and os.environ.get("TWILIO_WHATSAPP_FROM")),
    }


async def _resolve_audience(payload: NotificationBroadcastIn):
    flt = {}
    if payload.audience == "class_11":
        flt["class_level"] = "11th"
    elif payload.audience == "class_12":
        flt["class_level"] = "12th"
    elif payload.audience == "batch" and payload.batch_id:
        flt["batch_id"] = payload.batch_id
    elif payload.audience == "all_students":
        pass
    elif payload.audience == "parents":
        pass  # handled separately — uses parent_mobile / parent_email
    students = await db.students.find(flt, {"_id": 0}).to_list(5000)
    return students


@router.post("/admin/notifications/broadcast")
async def broadcast(payload: NotificationBroadcastIn, _admin=Depends(require_admin)):
    students = await _resolve_audience(payload)
    mocked = _channels_mocked()
    notif_id = new_id()
    # Persist in-app notification fan-out
    bulk = []
    for s in students:
        bulk.append({
            "id": new_id(),
            "broadcast_id": notif_id,
            "student_id": s["id"],
            "title": payload.title,
            "message": payload.message,
            "channels": payload.channels,
            "read": False,
            "created_at": iso(now_utc()),
        })
    if bulk:
        await db.notifications.insert_many(bulk)

    # Track outbound (MOCKED if no keys)
    outbound = []
    for s in students:
        for ch in payload.channels:
            if ch == "in_app":
                continue
            target = ""
            if ch == "email":
                target = s.get("email") or ""
            elif ch == "sms":
                target = s.get("mobile") or ""
            elif ch == "whatsapp":
                target = s.get("parent_mobile") or s.get("mobile") or ""
            if not target:
                continue
            outbound.append({
                "id": new_id(),
                "broadcast_id": notif_id,
                "student_id": s["id"],
                "channel": ch,
                "target": target,
                "title": payload.title,
                "message": payload.message,
                "status": "mocked" if mocked.get(ch) else "queued",
                "created_at": iso(now_utc()),
            })
    if outbound:
        await db.notification_outbound.insert_many(outbound)

    await db.activities.insert_one({
        "id": new_id(),
        "type": "broadcast_sent",
        "text": f"Broadcast '{payload.title}' → {len(students)} student(s) via {','.join(payload.channels)}",
        "created_at": iso(now_utc()),
    })
    return {
        "ok": True,
        "broadcast_id": notif_id,
        "recipients": len(students),
        "outbound_messages": len(outbound),
        "channels_mocked": [k for k, v in mocked.items() if v and k in payload.channels],
    }


@router.get("/admin/notifications/history")
async def history(_admin=Depends(require_admin)):
    rows = await db.notifications.aggregate([
        {"$sort": {"created_at": -1}},
        {"$group": {
            "_id": "$broadcast_id",
            "title": {"$first": "$title"},
            "message": {"$first": "$message"},
            "channels": {"$first": "$channels"},
            "created_at": {"$first": "$created_at"},
            "recipients": {"$sum": 1},
        }},
        {"$sort": {"created_at": -1}},
        {"$limit": 100},
    ])
    for r in rows:
        r["broadcast_id"] = r.pop("_id")
    return rows


@router.get("/student/notifications")
async def student_inbox(student=Depends(require_student)):
    rows = await db.notifications.find({"student_id": student["id"]}, {"_id": 0}).sort("created_at", -1).limit(100).to_list(100)
    return rows


@router.post("/student/notifications/{nid}/read")
async def mark_read(nid: str, student=Depends(require_student)):
    await db.notifications.update_one({"id": nid, "student_id": student["id"]}, {"$set": {"read": True}})
    return {"ok": True}
