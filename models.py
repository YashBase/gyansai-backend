"""Pydantic models for API requests/responses."""
from __future__ import annotations
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, EmailStr


# ---------- Auth ----------
class AdminLoginIn(BaseModel):
    email: EmailStr
    password: str


class StudentLoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    token: str
    user: Dict[str, Any]
    role: str


# ---------- Institute Settings ----------
class InstituteSettingsIn(BaseModel):
    name: Optional[str] = None
    tagline: Optional[str] = None
    logo_url: Optional[str] = None
    favicon_url: Optional[str] = None
    address: Optional[str] = None
    contact_number: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    upi_id: Optional[str] = None
    bank_account: Optional[str] = None
    bank_ifsc: Optional[str] = None
    bank_name: Optional[str] = None
    social: Optional[Dict[str, str]] = None
    theme_primary: Optional[str] = None
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    ga_id: Optional[str] = None


# ---------- Students ----------
class StudentIn(BaseModel):
    name: str
    username: str
    password: Optional[str] = "student123"
    email: Optional[str] = ""
    mobile: Optional[str] = ""
    parent_mobile: Optional[str] = ""
    school: Optional[str] = ""
    enrollment_no: Optional[str] = ""
    photo_url: Optional[str] = ""
    class_level: Optional[str] = ""  # "", "11th", "12th"
    batch_id: Optional[str] = ""
    signup_status: Optional[str] = "approved"  # approved | pending | rejected


class StudentUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    mobile: Optional[str] = None
    parent_mobile: Optional[str] = None
    school: Optional[str] = None
    enrollment_no: Optional[str] = None
    photo_url: Optional[str] = None
    status: Optional[str] = None  # active | suspended
    password: Optional[str] = None
    class_level: Optional[str] = None
    batch_id: Optional[str] = None
    signup_status: Optional[str] = None


class StudentSignupIn(BaseModel):
    name: str
    mobile: str
    username: Optional[str] = ""  # student-chosen; falls back to "u<mobile>" if blank
    parent_mobile: Optional[str] = ""
    email: Optional[str] = ""
    password: str
    class_level: str  # "11th" | "12th"
    batch_id: Optional[str] = ""
    school: Optional[str] = ""
    from_share_link: Optional[bool] = False
    target_exam_id: Optional[str] = ""


# ---------- Questions ----------
class QuestionOption(BaseModel):
    key: str  # A, B, C, D
    text: str


class QuestionIn(BaseModel):
    title: str
    description: Optional[str] = ""
    image_url: Optional[str] = ""
    subject: str
    chapter: Optional[str] = ""
    topic: Optional[str] = ""
    test_folder: Optional[str] = ""  # group questions by test name e.g. "JEE Mains 2024 Paper 1"
    difficulty: str = "medium"  # easy | medium | hard
    tags: List[str] = Field(default_factory=list)
    type: str = "mcq_single"  # mcq_single, mcq_multi, true_false, fill_blank, numerical, short, long, file
    options: List[QuestionOption] = Field(default_factory=list)
    correct_answer: Any = None  # string, list, number depending on type
    explanation: Optional[str] = ""
    marks: float = 4.0
    negative_marks: float = 1.0


# ---------- Exams ----------
class ExamIn(BaseModel):
    name: str
    description: Optional[str] = ""
    type: str = "mock"  # legacy field — kept for back-compat
    exam_type: Optional[str] = "mock"  # weekly | unit | chapter | mock | final
    exam_tag: Optional[str] = ""  # folder/category — e.g. JEE Mains, JEE Advanced, MHT-CET, NEET
    class_level: Optional[str] = ""  # "", "11th", "12th"
    batch_ids: List[str] = Field(default_factory=list)
    duration_minutes: int = 60
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    passing_marks: float = 0
    total_marks: float = 0  # computed if 0
    marking_mode: str = "custom"  # positive | custom | none
    default_marks: float = 4.0   # per-question positive marks fallback
    default_negative: float = 1.0  # per-question negative fallback
    instructions: Optional[str] = ""
    randomize: bool = False
    negative_marking: bool = True
    question_ids: List[str] = Field(default_factory=list)
    assigned_student_ids: List[str] = Field(default_factory=list)
    allowed_tab_switches: int = 3
    enable_webcam: bool = True
    is_published: bool = False
    price: float = 0.0  # 0 = free


# ---------- Exam Attempts ----------
class StartAttemptIn(BaseModel):
    exam_id: str


class SaveAnswerIn(BaseModel):
    attempt_id: str
    question_id: str
    answer: Any
    status: str = "answered"  # answered | review | not_answered


class SubmitAttemptIn(BaseModel):
    attempt_id: str


class TabSwitchLogIn(BaseModel):
    attempt_id: str
    violation_type: str = "tab_switch"  # tab_switch | fullscreen_exit | copy | paste | right_click


class SnapshotIn(BaseModel):
    attempt_id: str
    image_base64: str
    violation: Optional[str] = None  # "multi_face" | "no_face" | "looking_away" | None


# ---------- Courses ----------
class CourseChapter(BaseModel):
    id: Optional[str] = None
    title: str
    videos: List[Dict[str, str]] = Field(default_factory=list)  # {title, url}
    notes: List[Dict[str, str]] = Field(default_factory=list)
    assignments: List[Dict[str, str]] = Field(default_factory=list)


class CourseIn(BaseModel):
    name: str
    description: Optional[str] = ""
    cover_url: Optional[str] = ""
    price: float = 0
    subject: Optional[str] = ""
    chapters: List[CourseChapter] = Field(default_factory=list)
    is_published: bool = False


# ---------- Test Series ----------
class TestSeriesIn(BaseModel):
    name: str
    description: Optional[str] = ""
    cover_url: Optional[str] = ""
    price: float = 0
    exam_ids: List[str] = Field(default_factory=list)
    is_published: bool = True


# ---------- Payments ----------
class CheckoutIn(BaseModel):
    item_type: str  # course | test_series | exam
    item_id: str
    coupon: Optional[str] = None


class PaymentRequestIn(BaseModel):
    item_type: str  # course | test_series | exam
    item_id: str
    utr: Optional[str] = ""
    coupon: Optional[str] = None
    payer_name: Optional[str] = None
    note: Optional[str] = None


class PaymentDecisionIn(BaseModel):
    reason: Optional[str] = ""


# ---------- Proctor Recording ----------
class RecordingChunkIn(BaseModel):
    attempt_id: str
    data_base64: str
    mime_type: str = "video/webm"
    duration_ms: int = 0
    chunk_index: int = 0


# ---------- OCR ----------
class OcrRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"



# ---------- Quick-Assign Exam from Question Folder ----------
class QuickAssignExamIn(BaseModel):
    test_folder: str  # source — pull all questions tagged with this folder
    exam_name: str
    exam_tag: Optional[str] = ""  # JEE Mains / JEE Advanced / MHT-CET / NEET / ...
    class_level: Optional[str] = ""  # "", "11th", "12th"  — also used to filter target students
    duration_minutes: int = 60
    passing_marks: float = 0
    allowed_tab_switches: int = 3
    enable_webcam: bool = True
    negative_marking: bool = True
    randomize: bool = False
    is_published: bool = True
    instructions: Optional[str] = ""
    assigned_student_ids: List[str] = Field(default_factory=list)  # if empty + class_level set, auto-pull all students of that class
    auto_assign_class_students: bool = True


# ---------- Exam-Folder Manager (create/update exam from question bank folder) ----------
class FolderExamIn(BaseModel):
    folder_name: str  # required — becomes test_folder on tagged questions and test_folder_source on the exam
    exam_id: Optional[str] = None  # if provided → update; else create new
    exam_name: str
    description: Optional[str] = ""
    exam_tag: Optional[str] = ""
    class_level: Optional[str] = ""
    duration_minutes: int = 60
    passing_marks: float = 0
    allowed_tab_switches: int = 3
    enable_webcam: bool = True
    negative_marking: bool = True
    randomize: bool = False
    is_published: bool = True
    instructions: Optional[str] = "Read each question carefully."
    question_ids: List[str] = Field(default_factory=list)
    assigned_student_ids: List[str] = Field(default_factory=list)
    auto_assign_class_students: bool = True
    tag_questions_to_folder: bool = True


# ---------- Batches ----------
class BatchIn(BaseModel):
    name: str  # e.g. "Batch A"
    class_level: str  # "11th" | "12th"
    description: Optional[str] = ""
    schedule: Optional[str] = ""  # free text e.g. "Mon/Wed/Fri 6-8pm"
    teacher_id: Optional[str] = ""


# ---------- Teachers ----------
class TeacherIn(BaseModel):
    name: str
    email: EmailStr
    password: str
    mobile: Optional[str] = ""
    subjects: List[str] = Field(default_factory=list)  # e.g. ["11th Math", "12th Math"]


class TeacherLoginIn(BaseModel):
    email: EmailStr
    password: str


class TeacherUpdate(BaseModel):
    name: Optional[str] = None
    mobile: Optional[str] = None
    subjects: Optional[List[str]] = None
    status: Optional[str] = None
    password: Optional[str] = None


# ---------- Attendance ----------
class AttendanceMarkIn(BaseModel):
    date: str  # YYYY-MM-DD
    batch_id: Optional[str] = ""
    class_level: Optional[str] = ""
    entries: List[Dict[str, str]] = Field(default_factory=list)  # [{student_id, status: present|absent|late}]
    note: Optional[str] = ""


# ---------- Study Material ----------
class StudyMaterialIn(BaseModel):
    title: str
    description: Optional[str] = ""
    type: str = "notes"  # notes | formula_sheet | assignment | video | chapter_note
    class_level: Optional[str] = ""  # 11th | 12th
    chapter: Optional[str] = ""
    file_url: Optional[str] = ""  # external URL (S3/Drive/YouTube)
    is_published: bool = True


# ---------- Notifications ----------
class NotificationBroadcastIn(BaseModel):
    title: str
    message: str
    audience: str = "all_students"  # all_students | class_11 | class_12 | batch | parents
    batch_id: Optional[str] = ""
    channels: List[str] = Field(default_factory=lambda: ["in_app"])  # in_app | email | sms | whatsapp


# ---------- Independent Exam Helpers (Copy / Import) ----------
class ExamCloneIn(BaseModel):
    source_exam_id: str
    new_name: str


class ExamImportBankIn(BaseModel):
    exam_id: str
    question_ids: List[str] = Field(default_factory=list)
    chapter: Optional[str] = None  # optional: pull all questions matching chapter+class
    class_level: Optional[str] = None

    is_published: bool = True
    instructions: Optional[str] = "Read each question carefully."
    question_ids: List[str] = Field(default_factory=list)
    assigned_student_ids: List[str] = Field(default_factory=list)
    auto_assign_class_students: bool = False
    tag_questions_to_folder: bool = True  # also stamp each picked question.test_folder=folder_name
