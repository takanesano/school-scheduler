"""SQLite persistence layer.

One connection per request (FastAPI dependency).  Foreign keys are enforced,
and every mutating statement runs inside the caller's transaction.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "school.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS students (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teachers (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    -- max lessons this teacher takes on one calendar day; 0 = no limit
    max_lessons_per_day INTEGER NOT NULL DEFAULT 0
        CHECK (max_lessons_per_day >= 0)
);

CREATE TABLE IF NOT EXISTS subjects (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    capacity INTEGER NOT NULL DEFAULT 1 CHECK (capacity >= 1),
    -- max DISTINCT teachers in the room per timeslot; 0 = no limit
    teacher_capacity INTEGER NOT NULL DEFAULT 0 CHECK (teacher_capacity >= 0)
);

CREATE TABLE IF NOT EXISTS timeslots (
    id     TEXT PRIMARY KEY,
    date   TEXT NOT NULL,               -- ISO YYYY-MM-DD
    period INTEGER NOT NULL,
    label  TEXT NOT NULL DEFAULT '',
    -- soft cost per lesson scheduled here; 0 = no penalty
    penalty INTEGER NOT NULL DEFAULT 0 CHECK (penalty >= 0),
    UNIQUE (date, period)
);

CREATE TABLE IF NOT EXISTS teacher_subjects (
    teacher_id TEXT NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    PRIMARY KEY (teacher_id, subject_id)
);

-- Assignments: "this student's subject is taught by this teacher".
-- Purely a HARD rule: a (student, subject) with any rows here may only
-- be taught that subject by one of its assigned teachers; (student,
-- subject) pairs with no rows are free. A teacher may be assigned to
-- the same student for several subjects (one row per subject).
CREATE TABLE IF NOT EXISTS student_subject_teachers (
    student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    teacher_id TEXT NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    PRIMARY KEY (student_id, subject_id, teacher_id)
);

CREATE TABLE IF NOT EXISTS student_needs (
    student_id        TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    subject_id        TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    sessions          INTEGER NOT NULL CHECK (sessions >= 1),
    PRIMARY KEY (student_id, subject_id)
);

CREATE TABLE IF NOT EXISTS teacher_availability (
    teacher_id  TEXT NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    timeslot_id TEXT NOT NULL REFERENCES timeslots(id) ON DELETE CASCADE,
    PRIMARY KEY (teacher_id, timeslot_id)
);

CREATE TABLE IF NOT EXISTS student_availability (
    student_id  TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    timeslot_id TEXT NOT NULL REFERENCES timeslots(id) ON DELETE CASCADE,
    PRIMARY KEY (student_id, timeslot_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    subject_id  TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    teacher_id  TEXT NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    timeslot_id TEXT NOT NULL REFERENCES timeslots(id) ON DELETE CASCADE,
    -- user-locked lessons survive generate/clear and refuse moves
    locked      INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1))
);

-- undo history for MANUAL timetable edits: each row is a full JSON
-- snapshot of the lessons table taken just before one edit. Solver
-- runs (generate) and Clear schedule wipe the stack instead of
-- pushing to it.
CREATE TABLE IF NOT EXISTS undo_stack (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    label    TEXT NOT NULL,
    snapshot TEXT NOT NULL
);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI may open a connection on one thread
    # and use it on another (async endpoints).  Each request has its own
    # connection, which is never used concurrently, so this is safe.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Additive migrations for DBs created before a column existed
    (CREATE TABLE IF NOT EXISTS never alters an existing table)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rooms)")}
    if "teacher_capacity" not in cols:
        conn.execute("ALTER TABLE rooms ADD COLUMN teacher_capacity "
                     "INTEGER NOT NULL DEFAULT 0 "
                     "CHECK (teacher_capacity >= 0)")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(teachers)")}
    if "max_lessons_per_day" not in cols:
        conn.execute("ALTER TABLE teachers ADD COLUMN max_lessons_per_day "
                     "INTEGER NOT NULL DEFAULT 0 "
                     "CHECK (max_lessons_per_day >= 0)")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(timeslots)")}
    if "penalty" not in cols:
        conn.execute("ALTER TABLE timeslots ADD COLUMN penalty "
                     "INTEGER NOT NULL DEFAULT 0 CHECK (penalty >= 0)")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(lessons)")}
    if "locked" not in cols:
        conn.execute("ALTER TABLE lessons ADD COLUMN locked "
                     "INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1))")
    # Legacy assignments: the old per-(student, teacher) priority table.
    # Its priority-0 (hard) rows become per-subject rows — one for each
    # subject the teacher can teach — in student_subject_teachers; soft
    # priorities (1-9) had no hard meaning and are dropped. The legacy
    # table itself is the migration marker: it is removed afterwards, so
    # this runs exactly once (and again for restored old backups).
    legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='teacher_students'").fetchone()
    if legacy:
        conn.execute(
            "INSERT OR IGNORE INTO student_subject_teachers "
            "  (student_id, subject_id, teacher_id) "
            "SELECT ts.student_id, tsub.subject_id, ts.teacher_id "
            "FROM teacher_students ts "
            "JOIN teacher_subjects tsub ON tsub.teacher_id = ts.teacher_id "
            "WHERE ts.priority = 0")
        conn.execute("DROP TABLE teacher_students")
