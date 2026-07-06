"""Seed sample data: questions, exams, courses, test series."""
import asyncio
from core import db, new_id, now_utc, iso, seed_initial_data


SAMPLE_QUESTIONS = [
    {
        "title": "If sin θ + cos θ = 1, then sin 2θ equals?",
        "subject": "Mathematics",
        "chapter": "Trigonometry",
        "topic": "Identities",
        "difficulty": "medium",
        "tags": ["JEE Main", "Trigonometry"],
        "type": "mcq_single",
        "options": [
            {"key": "A", "text": "0"},
            {"key": "B", "text": "1"},
            {"key": "C", "text": "-1"},
            {"key": "D", "text": "1/2"},
        ],
        "correct_answer": "A",
        "explanation": "Squaring both sides: 1 + sin 2θ = 1 ⇒ sin 2θ = 0.",
        "marks": 4, "negative_marks": 1,
    },
    {
        "title": "The derivative of x^3 with respect to x is",
        "subject": "Mathematics",
        "chapter": "Calculus",
        "topic": "Derivatives",
        "difficulty": "easy",
        "tags": ["JEE Main"],
        "type": "mcq_single",
        "options": [
            {"key": "A", "text": "3x"},
            {"key": "B", "text": "3x^2"},
            {"key": "C", "text": "x^2"},
            {"key": "D", "text": "x^4/4"},
        ],
        "correct_answer": "B",
        "explanation": "d/dx[x^n] = n·x^(n-1).",
        "marks": 4, "negative_marks": 1,
    },
    {
        "title": "Integration of 1/x dx is",
        "subject": "Mathematics",
        "chapter": "Calculus",
        "topic": "Integration",
        "difficulty": "easy",
        "tags": ["JEE Main"],
        "type": "mcq_single",
        "options": [
            {"key": "A", "text": "x + C"},
            {"key": "B", "text": "ln|x| + C"},
            {"key": "C", "text": "-1/x^2 + C"},
            {"key": "D", "text": "e^x + C"},
        ],
        "correct_answer": "B",
        "explanation": "∫1/x dx = ln|x| + C.",
        "marks": 4, "negative_marks": 1,
    },
    {
        "title": "The unit of electric resistance is",
        "subject": "Physics",
        "chapter": "Current Electricity",
        "topic": "Resistance",
        "difficulty": "easy",
        "tags": ["NEET"],
        "type": "mcq_single",
        "options": [
            {"key": "A", "text": "Volt"},
            {"key": "B", "text": "Ohm"},
            {"key": "C", "text": "Ampere"},
            {"key": "D", "text": "Coulomb"},
        ],
        "correct_answer": "B",
        "explanation": "Resistance is measured in ohms (Ω).",
        "marks": 4, "negative_marks": 1,
    },
    {
        "title": "What is the value of acceleration due to gravity (g) at the surface of Earth (approx)?",
        "subject": "Physics",
        "chapter": "Gravitation",
        "topic": "Basics",
        "difficulty": "easy",
        "tags": ["NEET"],
        "type": "numerical",
        "options": [],
        "correct_answer": "9.8",
        "explanation": "Standard value of g ≈ 9.8 m/s².",
        "marks": 4, "negative_marks": 0,
    },
    {
        "title": "Which of the following are noble gases? (Select all that apply)",
        "subject": "Chemistry",
        "chapter": "Periodic Table",
        "topic": "Noble Gases",
        "difficulty": "medium",
        "tags": ["NEET"],
        "type": "mcq_multi",
        "options": [
            {"key": "A", "text": "Helium"},
            {"key": "B", "text": "Nitrogen"},
            {"key": "C", "text": "Argon"},
            {"key": "D", "text": "Oxygen"},
        ],
        "correct_answer": ["A", "C"],
        "explanation": "Helium and Argon are noble (group 18) gases.",
        "marks": 4, "negative_marks": 1,
    },
    {
        "title": "Water (H₂O) is a polar molecule.",
        "subject": "Chemistry",
        "chapter": "Chemical Bonding",
        "topic": "Polarity",
        "difficulty": "easy",
        "tags": ["NEET"],
        "type": "true_false",
        "options": [{"key": "true", "text": "True"}, {"key": "false", "text": "False"}],
        "correct_answer": "true",
        "explanation": "Due to bent geometry and electronegativity difference, water is polar.",
        "marks": 2, "negative_marks": 0,
    },
    {
        "title": "If f(x) = x² and g(x) = 2x+1, then f(g(2)) is",
        "subject": "Mathematics",
        "chapter": "Functions",
        "topic": "Composition",
        "difficulty": "medium",
        "tags": ["JEE Main"],
        "type": "mcq_single",
        "options": [
            {"key": "A", "text": "25"},
            {"key": "B", "text": "16"},
            {"key": "C", "text": "9"},
            {"key": "D", "text": "5"},
        ],
        "correct_answer": "A",
        "explanation": "g(2)=5; f(5)=25.",
        "marks": 4, "negative_marks": 1,
    },
    {
        "title": "The cell powerhouse is called",
        "subject": "Biology",
        "chapter": "Cell Biology",
        "topic": "Organelles",
        "difficulty": "easy",
        "tags": ["NEET"],
        "type": "mcq_single",
        "options": [
            {"key": "A", "text": "Nucleus"},
            {"key": "B", "text": "Mitochondria"},
            {"key": "C", "text": "Ribosome"},
            {"key": "D", "text": "ER"},
        ],
        "correct_answer": "B",
        "explanation": "Mitochondria produce ATP, hence powerhouse of cell.",
        "marks": 4, "negative_marks": 1,
    },
    {
        "title": "Speed of light in vacuum (in 10⁸ m/s)",
        "subject": "Physics",
        "chapter": "Optics",
        "topic": "Light",
        "difficulty": "easy",
        "tags": ["JEE", "NEET"],
        "type": "numerical",
        "options": [],
        "correct_answer": "3",
        "explanation": "≈ 3 × 10⁸ m/s.",
        "marks": 4, "negative_marks": 0,
    },
]


async def main():
    await seed_initial_data()
    if await db.questions.count_documents({}) >= len(SAMPLE_QUESTIONS):
        print("Questions already seeded")
    else:
        await db.questions.delete_many({})
        q_ids = []
        for q in SAMPLE_QUESTIONS:
            q = dict(q)
            q["id"] = new_id()
            q["created_at"] = iso(now_utc())
            await db.questions.insert_one(q)
            q_ids.append(q["id"])
        print(f"Seeded {len(q_ids)} questions")

    # Seed a sample exam
    if await db.exams.count_documents({"name": "JEE Main — Sample Mock Test"}) == 0:
        q_docs = await db.questions.find({}, {"_id": 0, "id": 1}).to_list(20)
        ids = [d["id"] for d in q_docs][:10]
        await db.exams.insert_one({
            "id": new_id(),
            "name": "JEE Main — Sample Mock Test",
            "description": "Free demo exam to showcase the Gyansai Test Portal experience.",
            "type": "mock",
            "duration_minutes": 30,
            "start_at": None,
            "end_at": None,
            "passing_marks": 20,
            "instructions": "Read carefully. +4 for correct, -1 for wrong. Numericals: no negative marking.",
            "randomize": False,
            "negative_marking": True,
            "question_ids": ids,
            "allowed_tab_switches": 3,
            "enable_webcam": True,
            "is_published": True,
            "price": 0,
            "created_at": iso(now_utc()),
        })
        print("Seeded sample exam")

    # Seed a sample course
    if await db.courses.count_documents({}) == 0:
        await db.courses.insert_one({
            "id": new_id(),
            "name": "JEE Mathematics — Complete Course",
            "description": "Full coverage of JEE Main & Advanced Mathematics syllabus with 200+ video lectures.",
            "cover_url": "https://images.unsplash.com/photo-1635372722656-389f87a941b7?w=800",
            "subject": "Mathematics",
            "price": 0,
            "chapters": [
                {"id": new_id(), "title": "Sets, Relations & Functions", "videos": [{"title": "Introduction", "url": "https://www.youtube.com/embed/dQw4w9WgXcQ"}], "notes": [], "assignments": []},
                {"id": new_id(), "title": "Calculus", "videos": [{"title": "Limits", "url": "https://www.youtube.com/embed/dQw4w9WgXcQ"}], "notes": [], "assignments": []},
            ],
            "is_published": True,
            "created_at": iso(now_utc()),
        })
        await db.courses.insert_one({
            "id": new_id(),
            "name": "NEET Physics — Crash Course",
            "description": "60-day intensive NEET Physics revision with 100+ MCQs daily.",
            "cover_url": "https://images.unsplash.com/photo-1532187863486-abf9dbad1b69?w=800",
            "subject": "Physics",
            "price": 2999,
            "chapters": [],
            "is_published": True,
            "created_at": iso(now_utc()),
        })
        print("Seeded courses")

    # Seed test series
    if await db.test_series.count_documents({}) == 0:
        exams = await db.exams.find({}, {"_id": 0, "id": 1}).to_list(10)
        await db.test_series.insert_one({
            "id": new_id(),
            "name": "JEE Main 2026 — All India Test Series",
            "description": "30 Full-length mocks + 90 chapter tests with All India Ranking.",
            "cover_url": "https://images.unsplash.com/photo-1543269865-cbf427effbad?w=800",
            "price": 4999,
            "exam_ids": [e["id"] for e in exams],
            "is_published": True,
            "created_at": iso(now_utc()),
        })
        print("Seeded test series")


if __name__ == "__main__":
    asyncio.run(main())
