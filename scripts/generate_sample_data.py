"""Regenerate sample_data/ — a large summer term.

Deterministic (fixed seed): 60 students, 10 teachers, 2026-07-21 to
2026-08-31 with Sundays off and 6 periods per day, one 12-seat hall.
Each student takes ~3 subjects with EXACTLY 5 sessions each (~900
lessons). All teachers teach Japanese / English / Social Studies; only
five of them also teach Math and Science.

Run from the project root:
    .venv/bin/python scripts/generate_sample_data.py

The script also verifies the dataset: every need must pass
check_input_problems, so the shipped sample is always solvable-looking
before the solver even starts.
"""
from __future__ import annotations

import datetime as dt
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.scheduler import check_input_problems  # noqa: E402
from app.main import load_dataset  # noqa: E402
from app import db  # noqa: E402
from app.load_sample import load_directory  # noqa: E402

OUT = ROOT / "sample_data"
rng = random.Random(20260801)

START, END = dt.date(2026, 7, 21), dt.date(2026, 8, 31)
PERIODS = {1: "09:00-10:10", 2: "10:20-11:30", 3: "11:40-12:50",
           4: "14:00-15:10", 5: "15:20-16:30", 6: "16:40-17:50"}
WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

SURNAMES = ["Sato", "Suzuki", "Takahashi", "Tanaka", "Ito", "Watanabe",
            "Yamamoto", "Nakamura", "Kobayashi", "Kato", "Yoshida",
            "Yamada", "Sasaki", "Yamaguchi", "Matsumoto", "Inoue",
            "Kimura", "Hayashi", "Shimizu", "Saito"]
GIVEN = ["Haruto", "Yuto", "Sota", "Yuki", "Hayato", "Ren", "Aoi", "Yui",
         "Mei", "Rin", "Hina", "Sakura", "Kaito", "Riku", "Mio",
         "Koharu", "Ichika", "Minato", "Tsumugi", "Itsuki"]

SUBJECTS = {"math": "Math", "eng": "English", "jpn": "Japanese",
            "sci": "Science", "soc": "Social Studies"}

N_STUDENTS, N_TEACHERS = 60, 10


def term_days():
    d = START
    while d <= END:
        if d.weekday() != 6:               # Sundays off
            yield d
        d += dt.timedelta(days=1)


def write(name, header, rows):
    text = header + "\n" + "".join(",".join(map(str, r)) + "\n" for r in rows)
    (OUT / name).write_text(text, encoding="utf-8")
    print(f"{name}: {len(rows)} rows")


def main():
    days = list(term_days())
    slots = [(f"{d:%m%d}-{p}", d, p) for d in days for p in PERIODS]

    # ---- people
    names = [f"{g} {s}" for s in SURNAMES for g in GIVEN]
    rng.shuffle(names)
    students = [(f"s{i+1:02d}", names[i]) for i in range(N_STUDENTS)]
    teachers = [(f"t{i+1:02d}", f"{SURNAMES[i]}-sensei")
                for i in range(N_TEACHERS)]

    # ---- teacher subjects: everyone teaches Japanese / English /
    # Social Studies; only the first five also teach Math and Science
    subj_ids = list(SUBJECTS)
    teacher_subjects = []
    for i, (tid, _) in enumerate(teachers):
        subs = ["jpn", "eng", "soc"]
        if i < 5:
            subs += ["math", "sci"]
        teacher_subjects += [(tid, su) for su in sorted(subs)]

    # ---- availability: contiguous period bands on a weekday pattern.
    # Needs average 12 sessions per student in total; keep availability
    # generous anyway so the solver has room to optimize.
    def band(min_width):
        lo = rng.choice([1, 1, 2])
        hi = rng.choice([4, 5, 6, 6])
        if hi - lo + 1 < min_width:
            lo, hi = 1, min_width
        return range(lo, hi + 1)

    teacher_avail = []
    for tid, _ in teachers:
        wdays = rng.sample(range(6), rng.choice([5, 5, 6, 6]))   # Mon..Sat
        periods = band(4)
        for sid, d, p in slots:
            if d.weekday() in wdays and p in periods:
                teacher_avail.append((tid, sid))

    student_days = {}
    student_avail = []
    for stid, _ in students:
        wdays = rng.sample(range(6), rng.choice([4, 5, 5, 6]))
        periods = band(3)
        for sid, d, p in slots:
            if d.weekday() in wdays and p in periods:
                student_avail.append((stid, sid))
        student_days[stid] = sum(1 for d in days if d.weekday() in wdays)

    # ---- needs: ~3 subjects per student (avg 3), EXACTLY 5 sessions
    # per subject. Availability is generous enough that 5 x subjects
    # always fits the 2-per-day capacity; assert it anyway.
    SESSIONS = 5
    needs = []
    for stid, _ in students:
        subjects_taken = rng.sample(subj_ids, rng.choice([2, 3, 3, 4]))
        assert SESSIONS * len(subjects_taken) <= 2 * student_days[stid] - 2, \
            f"{stid} cannot fit {len(subjects_taken)} x {SESSIONS} sessions"
        for su in subjects_taken:
            needs.append((stid, su, SESSIONS))

    write("students.csv", "id,name", students)
    write("teachers.csv", "id,name", teachers)
    write("subjects.csv", "id,name", sorted(SUBJECTS.items()))
    write("rooms.csv", "id,name,capacity", [("hall", "Main Hall", 12)])
    write("timeslots.csv", "id,date,period,label",
          [(sid, d.isoformat(), p, PERIODS[p]) for sid, d, p in slots])
    write("teacher_subjects.csv", "teacher_id,subject_id", teacher_subjects)
    write("student_needs.csv", "student_id,subject_id,sessions", needs)
    write("teacher_availability.csv", "teacher_id,timeslot_id", teacher_avail)
    write("student_availability.csv", "student_id,timeslot_id", student_avail)
    total = sum(n for (_, _, n) in needs)
    print(f"total sessions: {total}")
    print(f"subjects per student: {len(needs) / len(students):.2f}")
    print(f"sessions per (student, subject): {total / len(needs):.2f}")

    # ---- sanity: no need may be structurally impossible
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "check.db"
        load_directory(OUT, db_path)
        conn = db.connect(db_path)
        try:
            problems = check_input_problems(load_dataset(conn))
        finally:
            conn.close()
    if problems:
        for p in problems[:10]:
            print("PROBLEM:", p)
        sys.exit(1)
    print("input diagnostics: clean")


if __name__ == "__main__":
    main()
