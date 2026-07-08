import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import ExamIn


def test_exam_in_accepts_resource_link_fields():
    exam = ExamIn(
        name="Demo exam",
        answer_key_url="https://drive.google.com/answer-key",
        detailed_solution_url="https://drive.google.com/solutions",
        show_answer_key_to_students=True,
        show_detailed_solutions_to_students=True,
    )

    payload = exam.model_dump()

    assert payload["answer_key_url"] == "https://drive.google.com/answer-key"
    assert payload["detailed_solution_url"] == "https://drive.google.com/solutions"
    assert payload["show_answer_key_to_students"] is True
    assert payload["show_detailed_solutions_to_students"] is True
