"""Question bank routes — Complete CRUD, filters, image upload, pagination, OCR."""
import base64
import json
import logging
import re
import os
from pathlib import Path
from typing import Optional, List, Any
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, File
from core import (
    db,
    require_admin,
    new_id,
    now_utc,
    iso,
    EMERGENT_LLM_KEY,
    AWS_S3_BUCKET,
    AWS_S3_REGION,
    AWS_S3_ENDPOINT_URL,
    AWS_S3_UPLOAD_PREFIX,
    AWS_S3_PUBLIC_READ,
)
from models import QuestionIn, OcrRequest, QuickAssignExamIn, FolderExamIn


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/questions", tags=["questions"])

# Create uploads directory for local image storage
UPLOAD_DIR = Path(__file__).parent / "uploads" / "questions"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# HELPERS
# ============================================================

def _s3_safe_filename(filename: str) -> str:
    """Sanitize filename for S3."""
    filename = (filename or "").split("/")[-1].split("\\")[-1]
    name, dot, ext = filename.rpartition(".")
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name or "file")
    ext = re.sub(r"[^a-zA-Z0-9]+", "", ext)
    return f"{name}.{ext}" if ext else name


def _s3_object_url(key: str) -> str:
    """Generate S3 object URL."""
    if AWS_S3_ENDPOINT_URL:
        return f"{AWS_S3_ENDPOINT_URL.rstrip('/')}/{key}"
    if AWS_S3_REGION:
        return f"https://{AWS_S3_BUCKET}.s3.{AWS_S3_REGION}.amazonaws.com/{key}"
    return f"https://{AWS_S3_BUCKET}.s3.amazonaws.com/{key}"


def _local_upload_path(filename: str) -> tuple:
    """Generate local upload path and return (relative_url, full_path)."""
    safe_name = f"{new_id()}-{_s3_safe_filename(filename)}"
    full_path = UPLOAD_DIR / safe_name
    relative_url = f"/uploads/questions/{safe_name}"
    return relative_url, full_path


def _question_response(doc: dict, include_answer: bool = False) -> dict:
    """Clean question document for response."""
    doc.pop("_id", None)
    if not include_answer:
        doc.pop("correct_answer", None)
        doc.pop("explanation", None)
    return doc


def _parse_ocr_json(text: str) -> dict:
    """Parse OCR response JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"questions": []}


# ============================================================
# CRUD: List & Filter
# ============================================================

@router.get("")
async def list_questions(
    _admin=Depends(require_admin),
    subject: Optional[str] = None,
    chapter: Optional[str] = None,
    topic: Optional[str] = None,
    test_folder: Optional[str] = None,
    difficulty: Optional[str] = None,
    type: Optional[str] = None,
    q: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
):
    """List questions with optional filters & pagination."""
    flt = {}
    if subject:
        flt["subject"] = subject
    if chapter:
        flt["chapter"] = chapter
    if topic:
        flt["topic"] = topic
    if test_folder:
        flt["test_folder"] = test_folder
    if difficulty:
        flt["difficulty"] = difficulty
    if type:
        flt["type"] = type
    if q:
        flt["$or"] = [
            {"title": {"$regex": q, "$options": "i"}},
            {"description": {"$regex": q, "$options": "i"}},
            {"tags": {"$regex": q, "$options": "i"}},
        ]
    
    total = await db.questions.count_documents(flt)
    docs = await db.questions.find(flt, {"_id": 0}).sort("created_at", -1).limit(limit + skip).to_list(limit + skip)
    docs = docs[skip:skip+limit]
    docs = [_question_response(d) for d in docs]
    
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "questions": docs,
    }


@router.get("/meta")
async def question_meta(_admin=Depends(require_admin)):
    """Get metadata: subjects, chapters, topics, folders for filter dropdowns."""
    subjects = await db.questions.distinct("subject")
    chapters = await db.questions.distinct("chapter")
    topics = await db.questions.distinct("topic")
    test_folders = await db.questions.distinct("test_folder")
    total = await db.questions.count_documents({})
    return {
        "subjects": sorted(s for s in subjects if s),
        "chapters": sorted(c for c in chapters if c),
        "topics": sorted(t for t in topics if t),
        "test_folders": sorted(f for f in test_folders if f),
        "total": total,
    }


@router.get("/folders")
async def list_folders(_admin=Depends(require_admin)):
    """List all distinct question folders with stats and linked exam metadata."""
    folder_names = sorted([f for f in await db.questions.distinct("test_folder") if f])
    exam_folder_sources = [
        f for f in await db.exams.distinct("test_folder_source") if f and f not in folder_names
    ]
    folder_names = sorted(set([*folder_names, *exam_folder_sources]))

    out = []
    for fname in folder_names:
        qcount = await db.questions.count_documents({"test_folder": fname})
        exam = await db.exams.find_one(
            {"test_folder_source": fname},
            {"_id": 0},
            sort=[("created_at", -1)],
        )
        row = {"folder_name": fname, "question_count": qcount}
        if exam:
            row.update({
                "exam_id": exam.get("id"),
                "exam_name": exam.get("name"),
                "class_level": exam.get("class_level", ""),
                "exam_tag": exam.get("exam_tag", ""),
                "duration_minutes": exam.get("duration_minutes"),
                "assigned_count": len(exam.get("assigned_student_ids") or []),
                "is_published": bool(exam.get("is_published")),
            })
        out.append(row)
    return out


@router.delete("/folders/{folder_name}")
async def delete_folder(folder_name: str, _admin=Depends(require_admin)):
    """Remove the folder tag from all questions and delete the linked exam for this section card."""
    fname = folder_name.strip()
    if not fname:
        raise HTTPException(status_code=400, detail="folder_name is required")

    await db.questions.update_many({"test_folder": fname}, {"$set": {"test_folder": ""}})

    exams = await db.exams.find({"test_folder_source": fname}, {"_id": 0, "id": 1}).to_list(50)
    exam_ids = [e["id"] for e in exams]
    if exam_ids:
        await db.exams.delete_many({"id": {"$in": exam_ids}})
        await db.students.update_many(
            {"exam_ids": {"$in": exam_ids}},
            {"$pull": {"exam_ids": {"$in": exam_ids}}},
        )

    return {"ok": True, "exams_deleted": len(exam_ids)}


@router.post("/folder-exam")
async def upsert_folder_exam(payload: FolderExamIn, _admin=Depends(require_admin)):
    """Create or update exam linked to a question folder."""
    fname = (payload.folder_name or "").strip()
    if not fname:
        raise HTTPException(status_code=400, detail="folder_name is required")
    if not (payload.exam_name or "").strip():
        raise HTTPException(status_code=400, detail="exam_name is required")

    # Tag selected questions with this folder
    if payload.tag_questions_to_folder and payload.question_ids:
        await db.questions.update_many(
            {"id": {"$in": payload.question_ids}},
            {"$set": {"test_folder": fname}},
        )

    # Resolve target students
    target_ids = set(payload.assigned_student_ids or [])
    if payload.auto_assign_class_students and payload.class_level:
        class_match = await db.students.distinct("id", {
            "class_level": payload.class_level,
            "status": {"$ne": "suspended"},
        })
        target_ids = target_ids.union(set(class_match))

    target_ids = list(target_ids)

    # Upsert exam
    exam_id = payload.exam_id or new_id()
    exam_data = {
        "id": exam_id,
        "name": (payload.exam_name or "").strip(),
        "description": payload.description or "",
        "exam_tag": payload.exam_tag or "",
        "class_level": payload.class_level or "",
        "duration_minutes": payload.duration_minutes,
        "passing_marks": payload.passing_marks,
        "instructions": payload.instructions or "",
        "randomize": payload.randomize,
        "negative_marking": payload.negative_marking,
        "question_ids": payload.question_ids,
        "assigned_student_ids": target_ids,
        "allowed_tab_switches": payload.allowed_tab_switches,
        "enable_webcam": payload.enable_webcam,
        "is_published": payload.is_published,
        "price": 0.0,
        "test_folder_source": fname,
    }

    existing = await db.exams.find_one({"id": exam_id}, {"_id": 0})
    if existing:
        action = "updated"
        exam_data["created_at"] = existing.get("created_at", iso(now_utc()))
        exam_data["updated_at"] = iso(now_utc())
        await db.exams.update_one({"id": exam_id}, {"$set": exam_data})
    else:
        action = "created"
        exam_data["created_at"] = iso(now_utc())
        await db.exams.insert_one(exam_data)

    # Update student exam_ids
    if target_ids:
        await db.students.update_many(
            {"id": {"$in": target_ids}},
            {"$addToSet": {"exam_ids": exam_id}},
        )

    exam_data.pop("_id", None)
    return {
        "exam": exam_data,
        "questions_count": len(payload.question_ids),
        "assigned_count": len(target_ids),
        "action": action,
    }


@router.get("/{qid}")
async def get_question(qid: str, _admin=Depends(require_admin)):
    """Get single question by ID (includes answer)."""
    doc = await db.questions.find_one({"id": qid}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Question not found")
    return _question_response(doc, include_answer=True)


@router.get("/{qid}/prev")
async def get_prev_question(
    qid: str,
    subject: Optional[str] = None,
    chapter: Optional[str] = None,
    _admin=Depends(require_admin),
):
    """Get previous question by created_at."""
    current = await db.questions.find_one({"id": qid}, {"_id": 0})
    if not current:
        raise HTTPException(status_code=404, detail="Question not found")
    
    flt = {"created_at": {"$lt": current.get("created_at")}}
    if subject:
        flt["subject"] = subject
    if chapter:
        flt["chapter"] = chapter
    
    prev_doc = await db.questions.find(flt, {"_id": 0}).sort("created_at", -1).limit(1).to_list(1)
    if not prev_doc:
        raise HTTPException(status_code=404, detail="No previous question")
    return _question_response(prev_doc[0], include_answer=True)


@router.get("/{qid}/next")
async def get_next_question(
    qid: str,
    subject: Optional[str] = None,
    chapter: Optional[str] = None,
    _admin=Depends(require_admin),
):
    """Get next question by created_at."""
    current = await db.questions.find_one({"id": qid}, {"_id": 0})
    if not current:
        raise HTTPException(status_code=404, detail="Question not found")
    
    flt = {"created_at": {"$gt": current.get("created_at")}}
    if subject:
        flt["subject"] = subject
    if chapter:
        flt["chapter"] = chapter
    
    next_doc = await db.questions.find(flt, {"_id": 0}).sort("created_at", 1).limit(1).to_list(1)
    if not next_doc:
        raise HTTPException(status_code=404, detail="No next question")
    return _question_response(next_doc[0], include_answer=True)


# ============================================================
# CRUD: Create
# ============================================================

@router.post("")
async def create_question(data: QuestionIn, _admin=Depends(require_admin)):
    """Create a new question."""
    doc = data.model_dump()
    doc["options"] = [o.model_dump() if hasattr(o, 'model_dump') else o for o in (doc.get("options") or [])]
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    doc["updated_at"] = iso(now_utc())
    
    # Validate
    if not doc.get("title"):
        raise HTTPException(status_code=400, detail="Question title is required")
    if not doc.get("subject"):
        raise HTTPException(status_code=400, detail="Subject is required")
    
    await db.questions.insert_one(doc)
    return _question_response(doc, include_answer=True)


# ============================================================
# CRUD: Update
# ============================================================

@router.put("/{qid}")
async def update_question(qid: str, data: QuestionIn, _admin=Depends(require_admin)):
    """Update question by ID."""
    existing = await db.questions.find_one({"id": qid}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Question not found")
    
    update = data.model_dump()
    update["options"] = [o.model_dump() if hasattr(o, 'model_dump') else o for o in (update.get("options") or [])]
    update["updated_at"] = iso(now_utc())
    
    if not update.get("title"):
        raise HTTPException(status_code=400, detail="Question title is required")
    if not update.get("subject"):
        raise HTTPException(status_code=400, detail="Subject is required")
    
    res = await db.questions.update_one({"id": qid}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Question not found")
    
    updated = await db.questions.find_one({"id": qid}, {"_id": 0})
    return _question_response(updated, include_answer=True)


# ============================================================
# CRUD: Delete
# ============================================================

@router.delete("/{qid}")
async def delete_question(qid: str, _admin=Depends(require_admin)):
    """Delete question by ID."""
    res = await db.questions.delete_one({"id": qid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Question not found")
    return {"deleted": True, "id": qid}


# ============================================================
# Bulk Operations
# ============================================================

@router.post("/bulk-save")
async def bulk_save_questions(payload: dict, _admin=Depends(require_admin)):
    """Bulk insert questions."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be an object")
    items = payload.get("questions")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="questions must be an array")

    ids = []
    inserted_ids = []
    try:
        for raw_question in items:
            if not isinstance(raw_question, dict):
                raise HTTPException(status_code=400, detail="Each question must be an object")
            question = QuestionIn.model_validate(raw_question).model_dump()
            question["id"] = new_id()
            question["created_at"] = iso(now_utc())
            question["updated_at"] = iso(now_utc())
            question.setdefault("subject", "General")
            question.setdefault("difficulty", "medium")
            question.setdefault("type", "mcq_single")
            question.setdefault("marks", 4.0)
            question.setdefault("negative_marks", 1.0)
            await db.questions.insert_one(question)
            ids.append(question["id"])
            inserted_ids.append(question["id"])
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to bulk save questions")
        if inserted_ids:
            try:
                await db.questions.delete_many({"id": {"$in": inserted_ids}})
            except Exception:
                logger.exception("Failed to rollback")
        raise HTTPException(status_code=500, detail="Failed to save questions")

    return {"saved": len(ids), "ids": ids}


@router.delete("")
async def bulk_delete_questions(payload: dict, _admin=Depends(require_admin)):
    """Bulk delete questions by IDs."""
    ids = payload.get("ids", [])
    if not isinstance(ids, list) or len(ids) == 0:
        raise HTTPException(status_code=400, detail="ids must be a non-empty array")
    
    res = await db.questions.delete_many({"id": {"$in": ids}})
    return {"deleted": res.deleted_count, "requested": len(ids)}


# ============================================================
# Image Upload
# ============================================================

@router.post("/upload-image")
async def upload_question_image(file: UploadFile = File(...), _admin=Depends(require_admin)):
    """Upload question image to S3 or local storage."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are supported")
    
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")

    if not AWS_S3_BUCKET:
        # Fallback to local storage
        relative_url, full_path = _local_upload_path(file.filename)
        try:
            with open(full_path, "wb") as f:
                f.write(content)
            logger.info(f"Question image uploaded locally: {full_path}")
            return {"image_url": relative_url}
        except Exception as e:
            logger.error(f"Local upload failed: {e}")
            # Final fallback: data URL
            data_url = f"data:{file.content_type};base64,{base64.b64encode(content).decode('utf-8')}"
            return {"image_url": data_url}

    # Upload to S3
    key = f"{AWS_S3_UPLOAD_PREFIX.rstrip('/')}/{new_id()}-{_s3_safe_filename(file.filename)}"
    try:
        import boto3
        s3 = boto3.client(
            "s3",
            region_name=AWS_S3_REGION or None,
            endpoint_url=AWS_S3_ENDPOINT_URL or None,
        )
        put_args = {
            "Bucket": AWS_S3_BUCKET,
            "Key": key,
            "Body": content,
            "ContentType": file.content_type or "application/octet-stream",
        }
        if AWS_S3_PUBLIC_READ:
            put_args["ACL"] = "public-read"
        s3.put_object(**put_args)
        logger.info(f"Question image uploaded to S3: {key}")
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")
        raise HTTPException(status_code=502, detail=f"S3 upload failed: {e}")

    return {"image_url": _s3_object_url(key)}


# ============================================================
# OCR via OpenAI Vision
# ============================================================

OCR_SYSTEM = (
    "You are an expert OCR system specialized in extracting math, physics, chemistry and biology "
    "exam questions from photos and PDFs of JEE / NEET / MHT-CET papers. "
    "Extract structured questions only. If multiple questions are present, return all of them. "
    "Detect mathematical equations and render them in LaTeX inside $...$ delimiters when possible. "
    "Respond with STRICT JSON only — no markdown, no commentary."
)

OCR_USER_PROMPT = (
    "Extract every question you can see in this image. "
    "For each question return JSON with this exact schema:\n"
    "{\n"
    '  "questions": [\n'
    "    {\n"
    '      "title": "<question text>",\n'
    '      "description": "<additional context if any>",\n'
    '      "type": "mcq_single" | "mcq_multi" | "numerical" | "true_false" | "short",\n'
    '      "subject": "Mathematics" | "Physics" | "Chemistry" | "Biology" | "General",\n'
    '      "options": [{"key": "A", "text": "..."}, {"key": "B", "text": "..."}],\n'
    '      "correct_answer": "A" | ["A","C"] | "<numeric>" | "<text>",\n'
    '      "explanation": "<solution / reasoning if visible, else empty>",\n'
    '      "difficulty": "easy" | "medium" | "hard",\n'
    '      "marks": 4,\n'
    '      "negative_marks": 1\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "Return ONLY valid JSON. If no question is visible, return {\"questions\": []}."
)


@router.post("/ocr")
async def ocr_extract(payload: OcrRequest, _admin=Depends(require_admin)):
    """Extract questions from base64-encoded image using OpenAI Vision."""
    if not EMERGENT_LLM_KEY:
        raise HTTPException(status_code=500, detail="EMERGENT_LLM_KEY not configured")

    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
    except Exception as e:
        raise HTTPException(status_code=501, detail=f"OCR module not available: {e}")

    b64 = payload.image_base64
    if "," in b64 and b64.lstrip().lower().startswith("data:"):
        b64 = b64.split(",", 1)[1]

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"ocr-{new_id()}",
        system_message=OCR_SYSTEM,
    ).with_model("openai", "gpt-4o")

    image = ImageContent(image_base64=b64)
    try:
        reply = await chat.send_message(UserMessage(text=OCR_USER_PROMPT, file_contents=[image]))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OCR failed: {e}")

    parsed = _parse_ocr_json(reply or "")
    qs = parsed.get("questions") or []
    for q in qs:
        q.setdefault("type", "mcq_single")
        q.setdefault("subject", "General")
        q.setdefault("difficulty", "medium")
        q.setdefault("marks", 4.0)
        q.setdefault("negative_marks", 1.0)
        q.setdefault("options", [])
        q.setdefault("explanation", "")
    return {"questions": qs}


@router.post("/ocr/upload")
async def ocr_upload(
    file: Any = File(None),
    files: Any = File(None),
    settings: str = Form(None),
    _admin=Depends(require_admin),
):
    """Multipart helper: upload image/PDF and run OCR on each page."""
    uploads = file or files or []
    if not uploads:
        raise HTTPException(status_code=400, detail="No file uploaded")

    ocr_settings = {}
    if settings:
        try:
            ocr_settings = json.loads(settings)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Malformed OCR settings JSON")

    pages_processed = 0
    total_pages_count = 0
    all_qs: list = []

    async def process_upload(upload_file: UploadFile):
        content = await upload_file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        if len(content) > 16 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large (max 16MB)")

        mime = (upload_file.content_type or "").lower()
        fname = (upload_file.filename or "").lower()
        if not fname:
            raise HTTPException(status_code=400, detail="Uploaded file must have a filename")

        is_pdf = "pdf" in mime or fname.endswith(".pdf")
        if not is_pdf:
            if not mime.startswith("image/"):
                raise HTTPException(status_code=400, detail="Unsupported file type")
            b64 = base64.b64encode(content).decode("utf-8")
            return await ocr_extract(OcrRequest(image_base64=b64, mime_type=mime or "image/jpeg"))

        try:
            import fitz
        except Exception as e:
            logger.exception("PDF support unavailable")
            raise HTTPException(status_code=500, detail=f"PDF support unavailable: {e}")

        try:
            pdf = fitz.open(stream=content, filetype="pdf")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid PDF: {e}")

        pages = pdf.page_count
        max_pages = min(pages, 15)
        pdf_qs: list = []
        for i in range(max_pages):
            page = pdf.load_page(i)
            pix = page.get_pixmap(dpi=180)
            img_bytes = pix.tobytes("png")
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            try:
                res = await ocr_extract(OcrRequest(image_base64=b64, mime_type="image/png"))
                for q in res.get("questions", []):
                    q.setdefault("source_page", i + 1)
                pdf_qs.extend(res.get("questions", []))
            except HTTPException as exc:
                logger.warning("OCR extraction failed for PDF page %s of %s: %s", i + 1, fname, exc.detail)
                continue
        pdf.close()
        return {"questions": pdf_qs, "pages_processed": max_pages, "total_pages": pages}

    try:
        for upload_file in uploads:
            result = await process_upload(upload_file)
            if not result:
                continue
            all_qs.extend(result.get("questions", []))
            pages_processed += result.get("pages_processed", 0)
            total_pages_count += result.get("total_pages", 0)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error during OCR upload")
        raise HTTPException(status_code=500, detail="Unexpected OCR upload failure")

    return {"questions": all_qs, "pages_processed": pages_processed, "total_pages": total_pages_count}


@router.post("/ocr/import")
async def ocr_import(
    file: list[UploadFile] = File(None),
    files: list[UploadFile] = File(None),
    settings: str = Form(None),
    _admin=Depends(require_admin),
):
    """Alias for ocr_upload."""
    return await ocr_upload(file=file, files=files, settings=settings, _admin=_admin)


# ============================================================
# Advanced: Quick Assign Exam from Folder
# ============================================================

@router.post("/quick-assign-exam")
async def quick_assign_exam(payload: QuickAssignExamIn, _admin=Depends(require_admin)):
    """Create exam from question test_folder and assign to students."""
    if not payload.test_folder.strip():
        raise HTTPException(status_code=400, detail="test_folder is required")
    if not payload.exam_name.strip():
        raise HTTPException(status_code=400, detail="exam_name is required")

    # Get all questions in folder
    qids = await db.questions.distinct("id", {"test_folder": payload.test_folder.strip()})
    if not qids:
        raise HTTPException(status_code=404, detail=f"No questions found in folder '{payload.test_folder}'")

    # Determine target students
    student_ids: List[str] = list(payload.assigned_student_ids or [])
    if payload.auto_assign_class_students and payload.class_level:
        class_match = await db.students.distinct("id", {
            "class_level": payload.class_level,
            "status": {"$ne": "suspended"},
        })
        student_ids = list({*student_ids, *class_match})

    # Create exam
    exam = {
        "id": new_id(),
        "name": payload.exam_name.strip(),
        "description": f"Auto-generated from folder '{payload.test_folder}' ({len(qids)} questions)",
        "type": "mock",
        "exam_tag": payload.exam_tag or "",
        "class_level": payload.class_level or "",
        "duration_minutes": payload.duration_minutes,
        "passing_marks": payload.passing_marks,
        "instructions": payload.instructions or "Read each question carefully.",
        "randomize": payload.randomize,
        "negative_marking": payload.negative_marking,
        "question_ids": qids,
        "assigned_student_ids": student_ids,
        "allowed_tab_switches": payload.allowed_tab_switches,
        "enable_webcam": payload.enable_webcam,
        "is_published": payload.is_published,
        "price": 0.0,
        "created_at": iso(now_utc()),
    }
    await db.exams.insert_one(exam)
    exam.pop("_id", None)

    # Assign to students
    if student_ids:
        await db.students.update_many(
            {"id": {"$in": student_ids}},
            {"$addToSet": {"exam_ids": exam["id"]}},
        )

    return {
        "exam": exam,
        "questions_count": len(qids),
        "assigned_count": len(student_ids),
    }


