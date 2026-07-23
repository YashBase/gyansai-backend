"""Exam routes: admin CRUD, student start/save/submit, proctoring & results."""
import json
import random
import re
import base64
import logging
from io import BytesIO
from difflib import SequenceMatcher
from typing import Optional, List, Any
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from core import db, require_admin, require_student, get_current_user, new_id, now_utc, iso

INCH = 72
from models import (
    ExamIn, StartAttemptIn, SaveAnswerIn, SubmitAttemptIn,
    TabSwitchLogIn, SnapshotIn, RecordingChunkIn,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/exams", tags=["exams"])


def simpleSplit(text: str, font_name: str, font_size: int, max_width: float) -> List[str]:
    """Simple text wrapping for PDF (approximate)."""
    if not text:
        return []
    words = text.split()
    lines = []
    current_line = []
    for word in words:
        test_line = " ".join(current_line + [word])
        # Rough approximation: 1 char ≈ 0.05 inch at 12pt font
        estimated_width = len(test_line) * (font_size / 240) * INCH
        if estimated_width > max_width and current_line:
            lines.append(" ".join(current_line))
            current_line = [word]
        else:
            current_line.append(word)
    if current_line:
        lines.append(" ".join(current_line))
    return lines


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


SUBJECTIVE_TYPES = {"short", "long", "file"}


# ---------- Helpers ----------
def _strip(q: dict, include_answer: bool = False) -> dict:
    q.pop("_id", None)
    if not include_answer:
        q.pop("correct_answer", None)
        q.pop("explanation", None)
    return q


def _normalize_choice_tokens(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                text = item.strip()
            else:
                text = str(item).strip()
            if text:
                values.append(text.lower())
        return values
    if isinstance(value, dict):
        if "answer" in value:
            return _normalize_choice_tokens(value["answer"])
        if "value" in value:
            return _normalize_choice_tokens(value["value"])
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, (list, tuple, set)):
            return _normalize_choice_tokens(parsed)
        return [part.strip().lower() for part in re.split(r"[,;|]+", text) if part.strip()]
    return [str(value).strip().lower()]


def _normalize_answer(question: dict, answer: Any) -> Any:
    qtype = question.get("type", "mcq_single")
    if qtype == "mcq_multi":
        return _normalize_choice_tokens(answer)
    if qtype == "true_false":
        if isinstance(answer, bool):
            return "true" if answer else "false"
        if isinstance(answer, str):
            return answer.strip().lower()
    if qtype == "numerical":
        if isinstance(answer, str):
            return answer.strip()
    if isinstance(answer, str):
        return answer.strip()
    return answer


def _resolve_marking(question: dict, exam: Optional[dict] = None) -> tuple[float, float]:
    """Return effective positive/negative marks for a question.

    Older question docs use `marks` / `negative_marks`, while newer question forms
    persist `default_marks` / `default_negative_marks`. The exam-level defaults are
    used as the fallback when a question doesn't specify its own values.
    """
    marks = question.get("marks")
    if marks is None:
        marks = question.get("default_marks")
    if marks is None:
        marks = (exam or {}).get("default_marks", 4)

    negative = question.get("negative_marks")
    if negative is None:
        negative = question.get("default_negative_marks")
    if negative is None:
        negative = (exam or {}).get("default_negative", 1)

    return float(marks or 0), float(negative or 0)


def _is_correct(question: dict, answer: Any) -> bool:
    qtype = question.get("type", "mcq_single")
    correct = question.get("correct_answer")
    if answer is None or answer == "" or correct is None:
        return False
    if qtype == "mcq_multi":
        answer_tokens = _normalize_choice_tokens(answer)
        correct_tokens = _normalize_choice_tokens(correct)
        if not answer_tokens or not correct_tokens:
            return False
        return sorted(answer_tokens) == sorted(correct_tokens)
    if qtype == "numerical":
        try:
            return abs(float(answer) - float(correct)) < 1e-3
        except Exception:
            return str(answer).strip().lower() == str(correct).strip().lower()
    return str(answer).strip().lower() == str(correct).strip().lower()


# ---------- Admin: Exam CRUD ----------
@router.get("")
async def list_exams(user=Depends(get_current_user)):
    if user["role"] == "admin":
        exams = await db.exams.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    else:
        # Student sees: assigned (via student.exam_ids) OR published-free.
        # Published-free exams are filtered further:
        #   • if exam has assigned_student_ids set, only those students see it
        #   • if exam has class_level set, only matching students see it (student's class_level must match)
        student = await db.students.find_one({"id": user["id"]}, {"_id": 0})
        assigned_ids = (student or {}).get("exam_ids") or []
        student_class = (student or {}).get("class_level") or ""
        student_batch = (student or {}).get("batch_id") or ""
        candidates = await db.exams.find({"$or": [
            {"is_published": True, "price": 0},
            {"id": {"$in": assigned_ids}},
        ]}, {"_id": 0, "question_ids": 0}).sort("created_at", -1).to_list(1000)
        exams = []
        for e in candidates:
            in_assigned_purchase = e["id"] in assigned_ids
            target_students = e.get("assigned_student_ids") or []
            target_class = (e.get("class_level") or "").strip()
            target_batches = e.get("batch_ids") or []
            # Purchased / explicitly assigned always visible
            if in_assigned_purchase:
                exams.append(e)
                continue
            # Published-free path — apply class, batch & assigned filters
            if target_students and user["id"] not in target_students:
                continue
            if target_class and student_class and target_class != student_class:
                continue
            if target_batches and (not student_batch or student_batch not in target_batches):
                continue
            exams.append(e)
        # Mark which exams the student already attempted/submitted
        for e in exams:
            attempt = await db.attempts.find_one(
                {"exam_id": e["id"], "student_id": user["id"], "status": "submitted"}, {"_id": 0}
            )
            e["attempted"] = bool(attempt)
            e["last_score"] = attempt["score"] if attempt else None
            e["attempt_id"] = attempt["id"] if attempt else None
    return exams


@router.get("/{exam_id}")
async def get_exam(exam_id: str, _admin=Depends(require_admin)):
    e = await db.exams.find_one({"id": exam_id}, {"_id": 0})
    if not e:
        raise HTTPException(status_code=404, detail="Exam not found")
    return e


@router.post("")
async def create_exam(data: ExamIn, _admin=Depends(require_admin)):
    doc = data.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    try:
        await db.exams.insert_one(doc)
        doc.pop("_id", None)
        # Push exam id into each assigned student's exam_ids so it appears for them
        target_students = doc.get("assigned_student_ids") or []
        if target_students:
            await db.students.update_many(
                {"id": {"$in": target_students}},
                {"$addToSet": {"exam_ids": doc["id"]}},
            )
        await db.activities.insert_one({
            "id": new_id(),
            "type": "exam_created",
            "text": f"Exam '{doc['name']}' created",
            "created_at": iso(now_utc()),
        })
        return doc
    except Exception as exc:
        logger.exception("Failed to create exam")
        raise HTTPException(status_code=500, detail="Failed to create exam")


@router.put("/{exam_id}")
async def update_exam(exam_id: str, data: ExamIn, _admin=Depends(require_admin)):
    upd = data.model_dump()
    res = await db.exams.update_one({"id": exam_id}, {"$set": upd})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Exam not found")
    # Sync assigned_student_ids → students.exam_ids
    target_students = upd.get("assigned_student_ids") or []
    if target_students:
        await db.students.update_many(
            {"id": {"$in": target_students}},
            {"$addToSet": {"exam_ids": exam_id}},
        )
    # Remove exam_id from students no longer in assignment list
    await db.students.update_many(
        {"exam_ids": exam_id, "id": {"$nin": target_students}},
        {"$pull": {"exam_ids": exam_id}},
    )
    return await db.exams.find_one({"id": exam_id}, {"_id": 0})


@router.post("/{exam_id}/clone")
async def clone_exam(exam_id: str, _admin=Depends(require_admin)):
    """Explicit admin-only action to copy a previous exam. Default behaviour is always
    Create Blank — this endpoint is the ONLY way questions ever get inherited."""
    e = await db.exams.find_one({"id": exam_id}, {"_id": 0})
    if not e:
        raise HTTPException(status_code=404, detail="Exam not found")
    new_eid = new_id()
    e["id"] = new_eid
    e["name"] = e["name"] + " (Copy)"
    e["created_at"] = iso(now_utc())
    e["is_published"] = False
    # Clear all assignments — a clone shares only the question paper, not students/results
    e["assigned_student_ids"] = []
    e["start_at"] = None
    e["end_at"] = None
    await db.exams.insert_one(e)
    e.pop("_id", None)
    await db.activities.insert_one({
        "id": new_id(),
        "type": "exam_cloned",
        "text": f"Exam '{e['name']}' cloned (independent copy)",
        "created_at": iso(now_utc()),
    })
    return e


@router.post("/{exam_id}/import-from-bank")
async def import_from_bank(exam_id: str, payload: dict, _admin=Depends(require_admin)):
    """Admin-only: append questions from the Question Bank into an existing exam.
    Body: {question_ids?: [...], chapter?: str, class_level?: str}.
    Either explicit question_ids OR a chapter+class_level filter (both supported)."""
    exam = await db.exams.find_one({"id": exam_id}, {"_id": 0})
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    qids = list(payload.get("question_ids") or [])
    if payload.get("chapter"):
        flt = {"chapter": payload["chapter"]}
        if payload.get("class_level"):
            # class_level can map to subject prefix in the question bank; we keep it loose
            pass
        bank = await db.questions.distinct("id", flt)
        qids.extend(bank)
    qids = list({*(exam.get("question_ids") or []), *qids})
    await db.exams.update_one({"id": exam_id}, {"$set": {"question_ids": qids}})
    return {"ok": True, "question_count": len(qids)}


@router.post("/{exam_id}/share")
async def share_link(exam_id: str, request: Request, _admin=Depends(require_admin)):
    """Return shareable info: direct URL, WhatsApp deep-link, QR-target URL.
    The URL is a /exam/<exam_id> entry point that the student app routes."""
    exam = await db.exams.find_one({"id": exam_id}, {"_id": 0})
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    settings = await db.settings.find_one({}, {"_id": 0}) or {}
    import os as _os
    # Prefer institute website → FRONTEND_URL env → Origin/Referer header (from admin UI)
    base = (settings.get("website") or _os.environ.get("FRONTEND_URL") or "").rstrip("/")
    if not base:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin:
            from urllib.parse import urlparse
            p = urlparse(origin)
            if p.scheme and p.netloc:
                base = f"{p.scheme}://{p.netloc}"
    if not base:
        # Final fallback: use the request's own host (works when frontend & backend share host via ingress)
        base = f"{request.url.scheme}://{request.url.netloc}"
    relative = f"/exam/{exam_id}"
    full_url = base + relative
    msg = (
        f"You're invited to attempt *{exam.get('name')}* on Gyansai Maths Test Portal. "
        f"Click to join: {full_url}"
    )
    import urllib.parse as _u
    wa = "https://wa.me/?text=" + _u.quote(msg)
    mail = f"mailto:?subject={_u.quote('Exam Invite: ' + exam.get('name', ''))}&body={_u.quote(msg)}"
    return {"exam_id": exam_id, "url": full_url, "relative": relative, "message": msg, "whatsapp": wa, "email": mail}


@router.delete("/{exam_id}")
async def delete_exam(exam_id: str, _admin=Depends(require_admin)):
    await db.exams.delete_one({"id": exam_id})
    return {"ok": True}


@router.post("/{exam_id}/claim")
async def claim_exam(exam_id: str, user=Depends(get_current_user)):
    """Student-side: grant this published exam to the currently logged-in student.
    Used by the public share-link flow when a logged-in student lands on /exam/<id>."""
    if user.get("role") != "student":
        raise HTTPException(status_code=403, detail="Only students can claim exams")
    exam = await db.exams.find_one({"id": exam_id}, {"_id": 0})
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    if not exam.get("is_published"):
        raise HTTPException(status_code=403, detail="Exam is not currently open")
    await db.students.update_one({"id": user["id"]}, {"$addToSet": {"exam_ids": exam_id}})
    await db.exams.update_one({"id": exam_id}, {"$addToSet": {"assigned_student_ids": user["id"]}})
    return {"ok": True, "exam_id": exam_id}



# ---------- Student: Attempt flow ----------
@router.post("/start")
async def start_attempt(data: StartAttemptIn, student=Depends(require_student)):
    exam = await db.exams.find_one({"id": data.exam_id}, {"_id": 0})
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    if not exam.get("is_published"):
        raise HTTPException(status_code=403, detail="Exam not available")

    # Schedule enforcement
    now = datetime.now(timezone.utc)
    start_dt = _parse_iso(exam.get("start_at"))
    end_dt = _parse_iso(exam.get("end_at"))
    if start_dt and now < start_dt:
        raise HTTPException(status_code=403, detail=f"Exam opens at {exam['start_at']}")
    if end_dt and now > end_dt:
        raise HTTPException(status_code=403, detail="Exam window has closed")

    # Check existing in-progress attempt
    existing = await db.attempts.find_one({
        "exam_id": data.exam_id, "student_id": student["id"], "status": "in_progress"
    }, {"_id": 0})
    if existing:
        return existing

    # Already submitted? Don't allow re-attempt (one-shot for simplicity).
    submitted = await db.attempts.find_one({
        "exam_id": data.exam_id, "student_id": student["id"], "status": "submitted"
    }, {"_id": 0})
    if submitted:
        raise HTTPException(status_code=400, detail="You have already attempted this exam.")

    # Fetch questions
    q_ids = exam.get("question_ids") or []
    questions = await db.questions.find({"id": {"$in": q_ids}}, {"_id": 0}).to_list(1000)
    # Preserve order
    qmap = {q["id"]: q for q in questions}
    ordered = [qmap[i] for i in q_ids if i in qmap]
    if exam.get("randomize"):
        random.shuffle(ordered)

    normalized_questions = []
    total_marks = 0.0
    for q in ordered:
        mark_pos, mark_neg = _resolve_marking(q, exam)
        q_snapshot = dict(q)
        q_snapshot["marks"] = mark_pos
        q_snapshot["negative_marks"] = mark_neg
        normalized_questions.append(_strip(q_snapshot))
        total_marks += mark_pos

    attempt = {
        "id": new_id(),
        "exam_id": data.exam_id,
        "exam_name": exam["name"],
        "student_id": student["id"],
        "student_name": student.get("name"),
        "started_at": iso(now_utc()),
        "duration_minutes": exam.get("duration_minutes", 60),
        "allowed_tab_switches": exam.get("allowed_tab_switches", 3),
        "tab_switches": 0,
        "violations": [],
        "answers": {},  # qid -> {answer, status}
        "questions": normalized_questions,
        "status": "in_progress",
        "score": None,
        "max_score": round(total_marks, 2),
    }
    await db.attempts.insert_one(attempt)
    attempt.pop("_id", None)
    await db.activities.insert_one({
        "id": new_id(),
        "type": "exam_started",
        "text": f"{student.get('name')} started '{exam['name']}'",
        "created_at": iso(now_utc()),
    })
    return attempt


@router.get("/attempt/{attempt_id}")
async def get_attempt(attempt_id: str, student=Depends(require_student)):
    a = await db.attempts.find_one({"id": attempt_id, "student_id": student["id"]}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    return a


@router.post("/save")
async def save_answer(data: SaveAnswerIn, student=Depends(require_student)):
    a = await db.attempts.find_one({"id": data.attempt_id, "student_id": student["id"]}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if a["status"] != "in_progress":
        raise HTTPException(status_code=400, detail="Attempt already submitted")
    question = await db.questions.find_one({"id": data.question_id}, {"_id": 0})
    normalized_answer = _normalize_answer(question or {}, data.answer)
    await db.attempts.update_one(
        {"id": data.attempt_id},
        {"$set": {f"answers.{data.question_id}": {"answer": normalized_answer, "status": data.status}}},
    )
    return {"ok": True}


@router.post("/violation")
async def log_violation(data: TabSwitchLogIn, student=Depends(require_student)):
    a = await db.attempts.find_one({"id": data.attempt_id, "student_id": student["id"]}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if a["status"] != "in_progress":
        return {"ok": True, "auto_submit": False}
    violation = {
        "id": new_id(),
        "type": data.violation_type,
        "at": iso(now_utc()),
    }
    new_switches = a.get("tab_switches", 0) + (1 if data.violation_type == "tab_switch" else 0)
    allowed = a.get("allowed_tab_switches", 3)
    update = {"$push": {"violations": violation}}
    auto_submit = False
    if data.violation_type == "tab_switch":
        update["$set"] = {"tab_switches": new_switches}
        if new_switches >= allowed:
            auto_submit = True
    await db.attempts.update_one({"id": data.attempt_id}, update)
    if auto_submit:
        await _do_submit(data.attempt_id, reason="tab_switch_limit_exceeded")
    return {"ok": True, "auto_submit": auto_submit, "tab_switches": new_switches, "allowed": allowed}


@router.post("/snapshot")
async def store_snapshot(data: SnapshotIn, student=Depends(require_student)):
    a = await db.attempts.find_one({"id": data.attempt_id, "student_id": student["id"]}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    # Strip data URL prefix if present and cap size
    b64 = data.image_base64 or ""
    if "," in b64 and b64.lstrip().lower().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    # Hard cap ~200 KB base64 (= ~150 KB raw) per snapshot
    MAX_B64 = 200_000
    if len(b64) > MAX_B64:
        raise HTTPException(status_code=413, detail=f"Snapshot too large (max {MAX_B64} chars base64). Capture at lower resolution/quality.")
    snap = {
        "id": new_id(),
        "attempt_id": data.attempt_id,
        "student_id": student["id"],
        "at": iso(now_utc()),
        "violation": data.violation,
        "size_bytes": len(b64),
        "image_base64": b64,
    }
    await db.proctor_snapshots.insert_one(snap)
    logger.info(f"📸 Snapshot saved: {data.violation or 'periodic'} ({len(b64)} bytes) for attempt {data.attempt_id}")
    
    if data.violation == "baseline":
        # Reset started_at ONLY on the first baseline (never resets on reload).
        # Prevents students from refreshing mid-exam to gain extra time.
        prior_baselines = await db.proctor_snapshots.count_documents({
            "attempt_id": data.attempt_id, "violation": "baseline"
        })
        if prior_baselines <= 1:
            await db.attempts.update_one(
                {"id": data.attempt_id},
                {"$set": {"started_at": iso(now_utc())}},
            )
    elif data.violation:
        await db.attempts.update_one(
            {"id": data.attempt_id},
            {"$push": {"violations": {"id": new_id(), "type": data.violation, "at": iso(now_utc())}}},
        )
    snap.pop("_id", None)
    # Don't echo the image back
    snap.pop("image_base64", None)
    return snap


async def _do_submit(attempt_id: str, reason: Optional[str] = None) -> dict:
    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if a["status"] == "submitted":
        return a

    exam = await db.exams.find_one({"id": a["exam_id"]}, {"_id": 0})
    negative = (exam or {}).get("negative_marking", True)
    marking_mode = (exam or {}).get("marking_mode", "custom")
    # Need actual questions with answers
    qids = [q["id"] for q in a["questions"]]
    full = await db.questions.find({"id": {"$in": qids}}, {"_id": 0}).to_list(1000)
    full_map = {q["id"]: q for q in full}

    answers = a.get("answers", {})
    score = 0.0
    correct = 0
    wrong = 0
    skipped = 0
    pending = 0
    subject_stats: dict = {}
    per_q = []
    for q in a["questions"]:
        qid = q["id"]
        info = full_map.get(qid, {})
        ans = (answers.get(qid) or {}).get("answer")
        marks, neg = _resolve_marking(q, exam)
        if marking_mode in {"positive", "none"}:
            neg = 0
        elif not negative:
            neg = 0
        qtype = q.get("type", "mcq_single")
        result = "skipped"
        mark_got = 0
        if ans is None or ans == "" or ans == []:
            skipped += 1
        elif qtype in SUBJECTIVE_TYPES:
            # Attempt automatic evaluation when a model answer exists.
            # If no model answer is present, keep as pending for manual review.
            model_ans = info.get("correct_answer")
            given_text = ans if isinstance(ans, str) else (str(ans) if ans is not None else "")
            mark_got = 0
            if model_ans is not None and str(model_ans).strip() != "":
                # Use a simple sequence similarity heuristic to score subjective answers.
                try:
                    sim = SequenceMatcher(None, str(model_ans).strip().lower(), given_text.strip().lower()).ratio()
                except Exception:
                    sim = 0.0
                # Thresholds: >=0.75 -> full marks; >=0.4 -> partial (50%); else zero
                if sim >= 0.75:
                    mark_got = marks
                    result = "correct"
                elif sim >= 0.4:
                    mark_got = round(marks * 0.5, 2)
                    result = "partial"
                else:
                    mark_got = 0
                    result = "wrong"
            else:
                # No model answer available: fall back to manual review
                result = "pending_review"
                pending += 1
        elif _is_correct(info, ans):
            correct += 1
            mark_got = marks
            result = "correct"
        else:
            wrong += 1
            mark_got = -neg
            result = "wrong"
        score += mark_got
        subj = q.get("subject") or "General"
        s = subject_stats.setdefault(subj, {"correct": 0, "wrong": 0, "skipped": 0, "pending_review": 0, "score": 0})
        s[result] = s.get(result, 0) + 1
        s["score"] += mark_got
        per_q.append({
            "qid": qid, "result": result, "marks": mark_got,
            "max_marks": marks,
            "type": qtype,
            "correct_answer": info.get("correct_answer"),
            "given": ans,
            "explanation": info.get("explanation", ""),
            "comment": None,
        })

    update = {
        "status": "submitted",
        "submitted_at": iso(now_utc()),
        "score": round(score, 2),
        "correct": correct,
        "wrong": wrong,
        "skipped": skipped,
        "pending_review": pending,
        "has_pending_review": pending > 0,
        "subject_stats": subject_stats,
        "per_question": per_q,
        "submit_reason": reason or "manual",
    }
    await db.attempts.update_one({"id": attempt_id}, {"$set": update})
    await db.activities.insert_one({
        "id": new_id(),
        "type": "exam_submitted",
        "text": f"{a.get('student_name')} submitted '{a.get('exam_name')}' — {update['score']}/{a.get('max_score')}",
        "created_at": iso(now_utc()),
    })
    return await db.attempts.find_one({"id": attempt_id}, {"_id": 0})


@router.post("/submit")
async def submit_attempt(data: SubmitAttemptIn, student=Depends(require_student)):
    a = await db.attempts.find_one({"id": data.attempt_id, "student_id": student["id"]}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    return await _do_submit(data.attempt_id, reason="manual")


# ---------- Results & Rank ----------
@router.get("/result/{attempt_id}")
async def get_result(attempt_id: str, user=Depends(get_current_user)):
    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if user["role"] == "student" and a["student_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    if a["status"] != "submitted":
        raise HTTPException(status_code=400, detail="Attempt not submitted yet")

    # Compute rank
    siblings = await db.attempts.find(
        {"exam_id": a["exam_id"], "status": "submitted"}, {"_id": 0, "student_id": 1, "score": 1, "student_name": 1}
    ).sort("score", -1).to_list(5000)
    rank = next((i + 1 for i, s in enumerate(siblings) if s["student_id"] == a["student_id"]), None)
    total = len(siblings)
    a["rank"] = rank
    a["total_participants"] = total
    a["leaderboard"] = siblings[:10]
    exam = await db.exams.find_one({"id": a.get("exam_id")}, {"_id": 0}) or {}
    resources = {}
    if exam.get("show_answer_key_to_students") and exam.get("answer_key_url"):
        resources["answer_key_url"] = exam.get("answer_key_url")
    if exam.get("show_detailed_solutions_to_students") and exam.get("detailed_solution_url"):
        resources["detailed_solution_url"] = exam.get("detailed_solution_url")
    a["exam_resources"] = resources
    # Accuracy
    attempted = a.get("correct", 0) + a.get("wrong", 0)
    a["accuracy"] = round((a.get("correct", 0) / attempted) * 100, 2) if attempted else 0
    return a


@router.get("/result/{attempt_id}/paper")
async def download_question_paper(attempt_id: str, user=Depends(get_current_user)):
    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if user["role"] == "student" and a["student_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    if a["status"] != "submitted":
        raise HTTPException(status_code=400, detail="Attempt not submitted yet")

    def _new_page(canvas_obj, title_text, y_pos):
        canvas_obj.showPage()
        canvas_obj.setFont("Helvetica-Bold", 18)
        canvas_obj.drawString(margin, height - margin, title_text)
        canvas_obj.setFont("Helvetica", 12)
        canvas_obj.drawString(margin, height - margin - 22, f"Student: {a.get('student_name', '')}")
        canvas_obj.drawString(margin, height - margin - 38, f"Date: {(a.get('submitted_at') or '')[:10]}")
        return height - margin - 58

    margin = 40
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4

    width, height = A4
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, height - margin, f"{a.get('exam_name', 'Exam')} — Question Paper")
    c.setFont("Helvetica", 12)
    c.drawString(margin, height - margin - 22, f"Student: {a.get('student_name', '')}")
    c.drawString(margin, height - margin - 38, f"Date: {(a.get('submitted_at') or '')[:10]}")
    y = height - margin - 60

    questions = a.get("questions", [])
    qids = [q.get("id") for q in questions if q.get("id")]
    full_questions = await db.questions.find({"id": {"$in": qids}}, {"_id": 0}).to_list(1000)
    qmap = {q["id"]: q for q in full_questions}

    for idx, q in enumerate(questions):
        lines = simpleSplit(f"{idx + 1}. {q.get('title', '').strip()}", "Helvetica", 11, width - margin * 2)
        if not lines:
            lines = [f"{idx + 1}. "]
        for line in lines:
            if y < margin + 40:
                y = _new_page(c, f"{a.get('exam_name', 'Exam')} — Question Paper", y)
            c.drawString(margin, y, line)
            y -= 14

        desc = (q.get("description") or "").strip()
        if desc:
            desc_lines = simpleSplit(desc, "Helvetica", 10, width - margin * 2)
            for line in desc_lines:
                if y < margin + 40:
                    y = _new_page(c, f"{a.get('exam_name', 'Exam')} — Question Paper", y)
                c.drawString(margin + 16, y, line)
                y -= 12

        options = q.get("options") or []
        for opt in options:
            text = f"{opt.get('key', '')}. {opt.get('text', '').strip()}"
            opt_lines = simpleSplit(text, "Helvetica", 10, width - margin * 2 - 16)
            for line in opt_lines:
                if y < margin + 40:
                    y = _new_page(c, f"{a.get('exam_name', 'Exam')} — Question Paper", y)
                c.drawString(margin + 16, y, line)
                y -= 12

        image_url = q.get("image_url") or q.get("image") or q.get("image_url")
        if image_url:
            if y < margin + 60:
                y = _new_page(c, f"{a.get('exam_name', 'Exam')} — Question Paper", y)
            c.setFont("Helvetica-Oblique", 10)
            c.drawString(margin + 16, y, f"[Image: {image_url}]")
            c.setFont("Helvetica", 10)
            y -= 14

        y -= 10
        if y < margin + 40 and idx < len(questions) - 1:
            y = _new_page(c, f"{a.get('exam_name', 'Exam')} — Question Paper", y)

    # Answer key section
    if y < margin + 80:
        y = _new_page(c, f"{a.get('exam_name', 'Exam')} — Question Paper", y)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, y, "Answer Key")
    y -= 26
    c.setFont("Helvetica", 12)

    for idx, q in enumerate(questions):
        if y < margin + 60:
            y = _new_page(c, f"{a.get('exam_name', 'Exam')} — Question Paper", y)
        c.setFont("Helvetica-Bold", 12)
        title = f"{idx + 1}. {q.get('title', '').strip()}"
        title_lines = simpleSplit(title, "Helvetica-Bold", 12, width - margin * 2)
        for line in title_lines:
            c.drawString(margin, y, line)
            y -= 14

        full_q = qmap.get(q.get("id"), {})
        ans = full_q.get("correct_answer")
        if isinstance(ans, list):
            answer_text = ", ".join(str(x).upper() for x in ans)
        elif ans is None:
            answer_text = "(No answer available)"
        else:
            answer_text = str(ans)

        answer_lines = simpleSplit(f"Answer: {answer_text}", "Helvetica", 11, width - margin * 2)
        c.setFont("Helvetica", 11)
        for line in answer_lines:
            if y < margin + 40:
                y = _new_page(c, f"{a.get('exam_name', 'Exam')} — Question Paper", y)
            c.drawString(margin + 16, y, line)
            y -= 13

        explanation = (full_q.get("explanation") or "").strip()
        if explanation:
            explanation_lines = simpleSplit(f"Explanation: {explanation}", "Helvetica-Oblique", 10, width - margin * 2)
            c.setFont("Helvetica-Oblique", 10)
            for line in explanation_lines:
                if y < margin + 40:
                    y = _new_page(c, f"{a.get('exam_name', 'Exam')} — Question Paper", y)
                c.drawString(margin + 20, y, line)
                y -= 12
        y -= 10
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=question-paper-{attempt_id}.pdf"
    })


@router.get("/public/result/{attempt_id}")
async def _legacy_public_result(attempt_id: str):
    """Kept for backward compatibility — see /api/public/result/{id} in routes_public."""
    from routes_public import public_result_full
    return await public_result_full(attempt_id)


@router.get("/public/recording/{attempt_id}")
async def _legacy_public_recording(attempt_id: str):
    from routes_public import public_recording_full
    return await public_recording_full(attempt_id)


@router.get("/{exam_id}/leaderboard")
async def leaderboard(exam_id: str, _admin=Depends(require_admin)):
    rows = await db.attempts.find(
        {"exam_id": exam_id, "status": "submitted"},
        {"_id": 0, "student_id": 1, "student_name": 1, "score": 1, "correct": 1, "wrong": 1, "skipped": 1, "submitted_at": 1},
    ).sort("score", -1).to_list(5000)
    return rows


@router.get("/{exam_id}/analytics")
async def exam_analytics(exam_id: str, _admin=Depends(require_admin)):
    rows = await db.attempts.find({"exam_id": exam_id, "status": "submitted"}, {"_id": 0}).to_list(5000)
    if not rows:
        return {"count": 0, "highest": 0, "lowest": 0, "avg": 0, "pass_pct": 0, "subject_avg": {}}
    scores = [r.get("score") or 0 for r in rows]
    exam = await db.exams.find_one({"id": exam_id}, {"_id": 0}) or {}
    pass_marks = exam.get("passing_marks", 0)
    passed = sum(1 for s in scores if s >= pass_marks)
    subj_acc: dict = {}
    for r in rows:
        for subj, st in (r.get("subject_stats") or {}).items():
            d = subj_acc.setdefault(subj, {"score": 0, "n": 0})
            d["score"] += st.get("score", 0)
            d["n"] += 1
    subj_avg = {k: round(v["score"] / max(v["n"], 1), 2) for k, v in subj_acc.items()}
    return {
        "count": len(rows),
        "highest": max(scores),
        "lowest": min(scores),
        "avg": round(sum(scores) / len(scores), 2),
        "pass_pct": round(passed * 100 / len(rows), 2),
        "subject_avg": subj_avg,
        "answer_key_url": exam.get("answer_key_url", ""),
        "detailed_solution_url": exam.get("detailed_solution_url", ""),
        "show_answer_key_to_students": bool(exam.get("show_answer_key_to_students", False)),
        "show_detailed_solutions_to_students": bool(exam.get("show_detailed_solutions_to_students", False)),
    }



# ---------- Manual Evaluation (subjective answers) ----------
@router.get("/evaluation/pending")
async def list_pending_evaluations(_admin=Depends(require_admin)):
    """List attempts that have subjective answers awaiting manual review."""
    rows = await db.attempts.find(
        {"status": "submitted", "has_pending_review": True},
        {"_id": 0, "id": 1, "exam_id": 1, "exam_name": 1, "student_id": 1, "student_name": 1,
         "submitted_at": 1, "score": 1, "max_score": 1, "pending_review": 1},
    ).sort("submitted_at", -1).to_list(2000)
    return rows


@router.get("/evaluation/{attempt_id}")
async def get_evaluation(attempt_id: str, _admin=Depends(require_admin)):
    """Return only the subjective questions/answers for review."""
    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    qmap = {q["id"]: q for q in (a.get("questions") or [])}
    answers = a.get("answers") or {}
    full = await db.questions.find({"id": {"$in": list(qmap.keys())}}, {"_id": 0}).to_list(1000)
    full_map = {q["id"]: q for q in full}
    items = []
    for pq in a.get("per_question") or []:
        qtype = pq.get("type") or qmap.get(pq["qid"], {}).get("type")
        if qtype not in SUBJECTIVE_TYPES:
            continue
        info = full_map.get(pq["qid"], {})
        items.append({
            "qid": pq["qid"],
            "type": qtype,
            "title": info.get("title") or qmap.get(pq["qid"], {}).get("title"),
            "subject": info.get("subject"),
            "max_marks": pq.get("max_marks") or info.get("marks", 0),
            "given": (answers.get(pq["qid"]) or {}).get("answer"),
            "current_marks": pq.get("marks", 0),
            "comment": pq.get("comment"),
            "result": pq.get("result"),
            "model_answer": info.get("correct_answer"),
            "explanation": info.get("explanation", ""),
        })
    return {
        "attempt_id": a["id"],
        "exam_name": a.get("exam_name"),
        "student_name": a.get("student_name"),
        "submitted_at": a.get("submitted_at"),
        "items": items,
        "pending_count": sum(1 for i in items if i["result"] == "pending_review"),
    }


@router.post("/evaluation/{attempt_id}")
async def save_evaluation(attempt_id: str, payload: dict, _admin=Depends(require_admin)):
    """Save admin's marks/comments for subjective questions.

    body: {evaluations: [{qid, marks, comment}]}
    """
    evals = payload.get("evaluations") or []
    if not isinstance(evals, list):
        raise HTTPException(status_code=400, detail="evaluations must be a list")

    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")

    per_q = list(a.get("per_question") or [])
    eval_map = {e["qid"]: e for e in evals if "qid" in e}
    delta_score = 0.0

    for pq in per_q:
        if pq["qid"] not in eval_map:
            continue
        if pq.get("type") not in SUBJECTIVE_TYPES:
            raise HTTPException(status_code=400, detail=f"Question {pq['qid']} is not a subjective question")
        e = eval_map[pq["qid"]]
        new_marks = float(e.get("marks") or 0)
        max_m = float(pq.get("max_marks") or 0)
        if new_marks < 0 or new_marks > max_m:
            raise HTTPException(status_code=400, detail=f"Marks must be 0..{max_m}")
        delta_score += new_marks - float(pq.get("marks") or 0)
        pq["marks"] = new_marks
        pq["comment"] = e.get("comment") or ""
        pq["result"] = "correct" if new_marks >= max_m else ("wrong" if new_marks == 0 else "partial")

    new_total = round(float(a.get("score") or 0) + delta_score, 2)
    pending = sum(1 for pq in per_q if pq.get("result") == "pending_review")

    # Recompute subject stats fresh
    subject_stats: dict = {}
    correct = wrong = skipped = pending_count = 0
    for pq in per_q:
        # Determine subject from original question doc snapshot
        subj = "General"
        for q in a.get("questions") or []:
            if q["id"] == pq["qid"]:
                subj = q.get("subject") or "General"
                break
        s = subject_stats.setdefault(subj, {"correct": 0, "wrong": 0, "skipped": 0, "pending_review": 0, "partial": 0, "score": 0})
        r = pq.get("result", "skipped")
        s[r] = s.get(r, 0) + 1
        s["score"] = round(s.get("score", 0) + (pq.get("marks") or 0), 2)
        if r == "correct": correct += 1
        elif r == "wrong": wrong += 1
        elif r == "skipped": skipped += 1
        elif r == "pending_review": pending_count += 1

    await db.attempts.update_one(
        {"id": attempt_id},
        {"$set": {
            "per_question": per_q,
            "score": new_total,
            "has_pending_review": pending_count > 0,
            "pending_review": pending_count,
            "correct": correct,
            "wrong": wrong,
            "skipped": skipped,
            "subject_stats": subject_stats,
            "evaluated_at": iso(now_utc()),
        }},
    )
    await db.activities.insert_one({
        "id": new_id(),
        "type": "evaluation_saved",
        "text": f"Evaluation saved for '{a.get('student_name')}' — '{a.get('exam_name')}' (new score: {new_total})",
        "created_at": iso(now_utc()),
    })
    return await db.attempts.find_one({"id": attempt_id}, {"_id": 0})



# ---------- Continuous Video+Audio Recording ----------
MAX_CHUNK_B64 = 3_000_000  # ~2.25 MB raw per 30s chunk


@router.post("/recording-chunk")
async def upload_recording_chunk(data: RecordingChunkIn, student=Depends(require_student)):
    """Receive a video+audio chunk (e.g. 30-second WebM clip) from the exam portal."""
    a = await db.attempts.find_one(
        {"id": data.attempt_id, "student_id": student["id"]},
        {"_id": 0, "id": 1, "status": 1},
    )
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    b64 = data.data_base64 or ""
    if "," in b64 and b64.lstrip().lower().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    if len(b64) > MAX_CHUNK_B64:
        raise HTTPException(status_code=413, detail=f"Chunk too large (max {MAX_CHUNK_B64} chars base64)")
    chunk = {
        "id": new_id(),
        "attempt_id": data.attempt_id,
        "student_id": student["id"],
        "chunk_index": data.chunk_index,
        "duration_ms": data.duration_ms,
        "mime_type": data.mime_type or "video/webm",
        "size_bytes": len(b64),
        "data_base64": b64,
        "at": iso(now_utc()),
    }
    await db.proctor_recordings.insert_one(chunk)
    return {"id": chunk["id"], "at": chunk["at"], "size_bytes": chunk["size_bytes"]}


def _decode_chunk_response(chunk: dict) -> StreamingResponse:
    s = (chunk.get("data_base64") or "").strip()
    # Restore base64 padding if missing & ignore non-alphabet chars
    pad = (-len(s)) % 4
    s = s + ("=" * pad)
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:
        raw = b""
    return StreamingResponse(
        BytesIO(raw),
        media_type=chunk.get("mime_type", "video/webm"),
        headers={"Content-Disposition": f"inline; filename=chunk-{chunk.get('chunk_index')}.webm"},
    )


@router.get("/admin/attempts/{attempt_id}/recording")
async def admin_attempt_recording(attempt_id: str, _admin=Depends(require_admin)):
    """List recording chunks for an attempt (metadata only — no payload)."""
    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0, "id": 1})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    rows = await db.proctor_recordings.find(
        {"attempt_id": attempt_id},
        {"_id": 0, "data_base64": 0},
    ).sort("at", 1).to_list(5000)
    return rows


@router.get("/admin/attempts/{attempt_id}/recording/{chunk_id}")
async def admin_attempt_recording_chunk(attempt_id: str, chunk_id: str, _admin=Depends(require_admin)):
    chunk = await db.proctor_recordings.find_one({"id": chunk_id, "attempt_id": attempt_id}, {"_id": 0})
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return _decode_chunk_response(chunk)





# ---------- Admin: Attempts + Recordings + Sharing ----------
@router.get("/admin/attempts")
async def admin_list_attempts(
    _admin=Depends(require_admin),
    exam_id: Optional[str] = None,
    student_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
):
    """List attempts (newest first) for the admin Results & Recording page."""
    flt = {}
    if exam_id:
        flt["exam_id"] = exam_id
    if student_id:
        flt["student_id"] = student_id
    if status:
        flt["status"] = status
    rows = await db.attempts.find(
        flt,
        {"_id": 0, "id": 1, "exam_id": 1, "exam_name": 1, "student_id": 1, "student_name": 1,
         "status": 1, "score": 1, "max_score": 1, "submitted_at": 1, "started_at": 1,
         "tab_switches": 1, "allowed_tab_switches": 1, "has_pending_review": 1, "violations": 1,
         "submit_reason": 1},
    ).sort("submitted_at", -1).to_list(limit)
    for r in rows:
        r["violations_count"] = len(r.pop("violations", []) or [])
    return rows


@router.get("/admin/attempts/{attempt_id}")
async def admin_get_attempt(attempt_id: str, _admin=Depends(require_admin)):
    """Full attempt detail (admin) — same as student's GET /result/{id} but without auth restriction."""
    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if a.get("status") == "submitted":
        siblings = await db.attempts.find(
            {"exam_id": a["exam_id"], "status": "submitted"},
            {"_id": 0, "student_id": 1, "score": 1, "student_name": 1},
        ).sort("score", -1).to_list(5000)
        a["rank"] = next((i + 1 for i, s in enumerate(siblings) if s["student_id"] == a["student_id"]), None)
        a["total_participants"] = len(siblings)
        a["leaderboard"] = siblings[:10]
        attempted = a.get("correct", 0) + a.get("wrong", 0)
        a["accuracy"] = round((a.get("correct", 0) / attempted) * 100, 2) if attempted else 0
    snap_count = await db.proctor_snapshots.count_documents({"attempt_id": attempt_id})
    a["snapshots_count"] = snap_count
    return a


@router.get("/admin/attempts/{attempt_id}/snapshots")
async def admin_attempt_snapshots(attempt_id: str, _admin=Depends(require_admin)):
    """All proctoring snapshots for an attempt — includes base64 image bytes."""
    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0, "id": 1})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    snaps = await db.proctor_snapshots.find(
        {"attempt_id": attempt_id}, {"_id": 0},
    ).sort("at", 1).to_list(5000)
    return snaps


@router.delete("/admin/attempts/{attempt_id}")
async def admin_delete_attempt(attempt_id: str, _admin=Depends(require_admin)):
    """Cascade-delete an attempt and ALL associated proctoring data (snapshots, recording chunks, share events)."""
    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0, "id": 1, "exam_name": 1, "student_name": 1})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    snap_n = (await db.proctor_snapshots.delete_many({"attempt_id": attempt_id})).deleted_count
    rec_n = (await db.proctor_recordings.delete_many({"attempt_id": attempt_id})).deleted_count
    shr_n = (await db.share_events.delete_many({"attempt_id": attempt_id})).deleted_count
    await db.attempts.delete_one({"id": attempt_id})
    await db.activities.insert_one({
        "id": new_id(),
        "type": "attempt_deleted",
        "text": f"Deleted attempt '{a.get('exam_name')}' by {a.get('student_name')} (snapshots={snap_n}, clips={rec_n})",
        "created_at": iso(now_utc()),
    })
    return {"ok": True, "snapshots_deleted": snap_n, "recordings_deleted": rec_n, "shares_deleted": shr_n}



@router.post("/admin/attempts/{attempt_id}/share")
async def admin_share_attempt(attempt_id: str, payload: dict = None, _admin=Depends(require_admin)):
    """Log a share event and return shareable links + pre-built parent message.

    Payload (optional): {channel: 'whatsapp'|'email'|'sms'|'copy', recipient: '<phone or email>'}
    """
    payload = payload or {}
    channel = (payload.get("channel") or "copy").lower()
    recipient = payload.get("recipient") or ""

    a = await db.attempts.find_one({"id": attempt_id}, {"_id": 0})
    if not a:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if a.get("status") != "submitted":
        raise HTTPException(status_code=400, detail="Attempt not yet submitted")

    settings = await db.institute_settings.find_one({"id": "default"}, {"_id": 0}) or {}
    inst_name = settings.get("name") or "Gyansai Maths IIT Center"

    public_path = f"/r/{attempt_id}"
    cert_path = f"/api/public/certificate/{attempt_id}"

    msg = (
        f"Hello! Here is the result of {a.get('student_name')} for "
        f"\"{a.get('exam_name')}\" at {inst_name}:\n"
        f"Score: {a.get('score')} / {a.get('max_score')}\n"
        f"View full result: {{base}}{public_path}\n"
        f"Certificate: {{base}}{cert_path}"
    )

    share = {
        "id": new_id(),
        "attempt_id": attempt_id,
        "channel": channel,
        "recipient": recipient,
        "shared_at": iso(now_utc()),
        "public_path": public_path,
        "certificate_path": cert_path,
        "message_template": msg,
    }
    await db.share_events.insert_one(share)
    await db.activities.insert_one({
        "id": new_id(),
        "type": "result_shared",
        "text": f"Result of '{a.get('student_name')}' shared via {channel}{f' to {recipient}' if recipient else ''}",
        "created_at": iso(now_utc()),
    })
    share.pop("_id", None)
    return share
