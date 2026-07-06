"""Authentication routes — admin & student login."""
from fastapi import APIRouter, HTTPException, Depends
from core import db, verify_password, create_token, get_current_user, clean_doc
from models import AdminLoginIn, StudentLoginIn, TokenOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/admin/login", response_model=TokenOut)
async def admin_login(data: AdminLoginIn):
    admin = await db.admins.find_one({"email": data.email.lower()})
    if not admin or not verify_password(data.password, admin["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(admin["id"], "admin")
    clean_doc(admin)
    return {"token": token, "user": admin, "role": "admin"}


@router.post("/student/login", response_model=TokenOut)
async def student_login(data: StudentLoginIn):
    student = await db.students.find_one({"username": data.username})
    if not student or not verify_password(data.password, student["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if student.get("status") == "suspended":
        raise HTTPException(status_code=403, detail="Account suspended. Contact admin.")
    if student.get("signup_status") == "pending":
        raise HTTPException(status_code=403, detail="Account pending admin approval. Please try again after approval.")
    if student.get("signup_status") == "rejected":
        raise HTTPException(status_code=403, detail="Signup was rejected. Contact admin.")
    token = create_token(student["id"], "student")
    clean_doc(student)
    return {"token": token, "user": student, "role": "student"}


@router.get("/me")
async def get_me(user=Depends(get_current_user)):
    return user
