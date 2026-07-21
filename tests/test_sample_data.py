"""The shipped sample data must import cleanly and solve completely.

The sample is deliberately large (a stress-test term): ~60 students,
10 teachers, 2026-08-01..2026-08-27 minus Sundays × 5 periods, one
30-seat hall, ~12 sessions per student in total across their subjects
(~700 lessons). Regenerate it with scripts/generate_sample_data.py.
"""
from pathlib import Path

from app import db
from app.load_sample import load_directory
from app.main import load_dataset
from app.scheduler import (check_input_problems, coverage_report, solve,
                           validate)

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


def test_sample_data_imports_and_solves(tmp_path):
    db_path = tmp_path / "sample.db"
    counts = load_directory(SAMPLE_DIR, db_path)
    assert counts["students"] == 60
    assert counts["teachers"] == 10
    assert counts["rooms"] == 1
    assert counts["timeslots"] == 115   # 23 days (Sundays off) x 5 periods

    conn = db.connect(db_path)
    try:
        data = load_dataset(conn)
    finally:
        conn.close()

    # ~12 sessions per STUDENT in total (all subjects), as specified
    mean = sum(data.student_needs.values()) / len(data.students)
    assert 11 <= mean <= 13, mean

    assert check_input_problems(data) == []
    result = solve(data)
    assert result.complete, f"unscheduled: {result.unscheduled}"
    assert len(result.lessons) == sum(data.student_needs.values())
    assert validate(data, result.lessons) == []
    assert coverage_report(data, result.lessons) == []
