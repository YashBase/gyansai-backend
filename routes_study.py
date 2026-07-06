"""Study Material routes — PDF notes, formula sheets, video lectures."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from core import db, require_admin, require_student, new_id, now_utc, iso
from models import StudyMaterialIn

router = APIRouter(tags=["study_material"])


@router.get("/admin/study-material")
async def admin_list_study(_admin=Depends(require_admin), class_level: Optional[str] = None):
    flt = {}
    if class_level:
        flt["class_level"] = class_level
    return await db.study_materials.find(flt, {"_id": 0}).sort("created_at", -1).to_list(500)


@router.post("/admin/study-material")
async def create_study(data: StudyMaterialIn, _admin=Depends(require_admin)):
    doc = data.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.study_materials.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.put("/admin/study-material/{mid}")
async def update_study(mid: str, data: StudyMaterialIn, _admin=Depends(require_admin)):
    res = await db.study_materials.update_one({"id": mid}, {"$set": data.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Material not found")
    return await db.study_materials.find_one({"id": mid}, {"_id": 0})


@router.delete("/admin/study-material/{mid}")
async def delete_study(mid: str, _admin=Depends(require_admin)):
    await db.study_materials.delete_one({"id": mid})
    return {"ok": True}


@router.get("/student/study-material")
async def student_list_study(student=Depends(require_student), class_level: Optional[str] = None):
    """Student sees published material — filtered by their class if known."""
    sdoc = await db.students.find_one({"id": student["id"]}, {"_id": 0})
    cls = (sdoc or {}).get("class_level") or class_level or ""
    flt = {"is_published": True}
    if cls:
        flt["$or"] = [{"class_level": cls}, {"class_level": ""}, {"class_level": None}]
    return await db.study_materials.find(flt, {"_id": 0}).sort("created_at", -1).to_list(500)
