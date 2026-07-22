"""The shipped sample data must import cleanly and solve completely.

The sample is a large generated term: 60 students, 10 teachers,
2026-07-21..2026-08-31 minus Sundays × 6 periods (216 slots), one
12-seat hall, ~3 subjects per student with EXACTLY 5 sessions each
(~900 lessons).
All teachers teach Japanese/English/Social Studies; only five also
teach Math and Science. Regenerate with
scripts/generate_sample_data.py.
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
    assert counts["timeslots"] == 216  # 36 days (Sundays off) x 6 periods
    # 10 teachers x (jpn, eng, soc) + 5 teachers x (math, sci)
    assert counts["teacher_subjects"] == 40

    conn = db.connect(db_path)
    try:
        data = load_dataset(conn)
    finally:
        conn.close()

    assert data.rooms["hall"].capacity == 12

    # only five teachers may teach math / science
    math_sci = {t for (t, su) in data.teacher_subjects
                if su in ("math", "sci")}
    assert len(math_sci) == 5
    core = {t for (t, su) in data.teacher_subjects
            if su in ("jpn", "eng", "soc")}
    assert len(core) == 10

    # ~3 subjects per student; EXACTLY 5 sessions per subject
    subj_per_student = len(data.student_needs) / len(data.students)
    assert 2.6 <= subj_per_student <= 3.4, subj_per_student
    assert all(n == 5 for n in data.student_needs.values())

    assert check_input_problems(data) == []
    result = solve(data)
    assert result.complete, f"unscheduled: {result.unscheduled}"
    assert len(result.lessons) == sum(data.student_needs.values())
    assert validate(data, result.lessons) == []
    assert coverage_report(data, result.lessons) == []
    # scheduled math/sci lessons only ever use the five capable teachers
    assert {l.teacher_id for l in result.lessons
            if l.subject_id in ("math", "sci")} <= math_sci
