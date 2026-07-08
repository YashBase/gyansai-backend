"""Gyansai Maths IIT Center — main FastAPI app."""

from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import os
import re

from core import close_mongo_client

try:
    from routes_auth import router as auth_router
    from routes_admin import router as admin_router
    from routes_questions import router as questions_router
    from routes_exams import router as exams_router
    from routes_student import router as student_router
    from routes_public import router as public_router
    from routes_batches import router as batches_router
    from routes_teachers import router as teachers_router
    from routes_attendance import router as attendance_router
    from routes_study import router as study_router
    from routes_notifications import router as notifications_router
    from routes_signup import router as signup_router
except Exception as exc:
    logging.warning("Some route modules failed to import during startup: %s", exc)
    auth_router = admin_router = questions_router = exams_router = student_router = public_router = batches_router = teachers_router = attendance_router = study_router = notifications_router = signup_router = APIRouter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

    close_mongo_client()


app = FastAPI(
    title="Gyansai Maths IIT Center API",
    lifespan=lifespan,
)

# -----------------------------
# CORS
# -----------------------------

origins = [
    "http://localhost:3000",
    "http://localhost:8080",
    "https://gyansai-frontend.vercel.app/",
    "https://gyansai-backend.vercel.app/"
    
]

@app.middleware("http")
async def cors_middleware(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method == "OPTIONS" and "access-control-request-method" in request.headers:
        response = Response(status_code=204)
        if origin and (origin in origins or re.match(r"https://([a-z0-9-]+\.)*vercel\.app", origin)):
            response.headers["access-control-allow-origin"] = origin
            response.headers["access-control-allow-credentials"] = "true"
            response.headers["access-control-allow-methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            response.headers["access-control-allow-headers"] = request.headers.get("access-control-request-headers", "authorization, content-type")
            response.headers["access-control-max-age"] = "86400"
        return response

    response = await call_next(request)
    if origin and (origin in origins or re.match(r"https://([a-z0-9-]+\.)*vercel\.app", origin)):
        response.headers["access-control-allow-origin"] = origin
        response.headers["access-control-allow-credentials"] = "true"
        response.headers["vary"] = "Origin"
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins + ["*"],
    allow_origin_regex=r"https://([a-z0-9-]+\.)*vercel\.app",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# -----------------------------
# API Router
# -----------------------------

api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {
        "app": "Gyansai Maths IIT Center",
        "status": "ok",
    }


@api_router.get("/health")
async def health():
    return {"ok": True, "service": "gyansai-backend"}


api_router.include_router(auth_router)
api_router.include_router(admin_router)
api_router.include_router(questions_router)
api_router.include_router(exams_router)
api_router.include_router(student_router)
api_router.include_router(public_router)
api_router.include_router(batches_router)
api_router.include_router(teachers_router)
api_router.include_router(attendance_router)
api_router.include_router(study_router)
api_router.include_router(notifications_router)
api_router.include_router(signup_router)

app.include_router(api_router)

# -----------------------------
# Logging
# -----------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, exc: Exception):
    logging.exception(
        "Unhandled exception for %s %s",
        request.method,
        request.url,
    )

    content = {"detail": "Internal server error. Please check server logs."}
    response = JSONResponse(status_code=500, content=content)

    # Ensure CORS headers are present on error responses so browsers don't block them.
    origin = request.headers.get("origin")
    try:
        if origin and (origin in origins or re.match(r"https://([a-z0-9-]+\.)*vercel\.app", origin)):
            response.headers["access-control-allow-origin"] = origin
            response.headers["access-control-allow-credentials"] = "true"
            response.headers["vary"] = "Origin"
    except Exception:
        # Don't let header-setting errors mask the original exception
        pass

    return response
