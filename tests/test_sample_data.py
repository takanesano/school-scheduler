"""The shipped sample data must import cleanly and solve completely."""
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
    assert counts["students"] == 6
    assert counts["timeslots"] == 34   # two-week summer term, no Sundays
    assert counts["student_needs"] == 12

    conn = db.connect(db_path)
    try:
        data = load_dataset(conn)
    finally:
        conn.close()

    assert check_input_problems(data) == []
    result = solve(data)
    assert result.complete, f"unscheduled: {result.unscheduled}"
    assert len(result.lessons) == 30  # total sessions across all needs
    assert validate(data, result.lessons) == []
    assert coverage_report(data, result.lessons) == []
