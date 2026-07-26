"""FastAPI application: REST API + static web UI.

Run locally with:  .venv/bin/uvicorn app.main:app --reload
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
from pathlib import Path

from fastapi import (Depends, FastAPI, File, HTTPException, Response,
                     UploadFile)
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import csv_io, db, views
from .scheduler import (OBJECTIVE_TERMS, Dataset, Lesson, Room, Timeslot,
                        check_input_problems, coverage_report,
                        optimize_teacher_days, schedule_objective, solve,
                        student_day_stats, teacher_day_stats, validate)
from .solver_v2 import ObjectiveWeights, SolverConfig, solve_v2

from contextlib import asynccontextmanager


def _migrate_settings(conn: sqlite3.Connection) -> None:
    """One-time migration: consecutiveness used to be a boolean setting
    (`require_consecutive`); it is now the `student_day_gap` objective
    cap. Fold a legacy row's intent into objective_caps and delete it —
    the legacy row's presence is the migration marker."""
    row = conn.execute("SELECT value FROM settings "
                       "WHERE key = 'require_consecutive'").fetchone()
    if row is None:
        return
    caps_row = conn.execute("SELECT value FROM settings "
                            "WHERE key = 'objective_caps'").fetchone()
    try:
        caps = json.loads(caps_row["value"]) if caps_row else {}
    except (ValueError, TypeError):
        caps = {}
    if row["value"] == "1":
        caps.setdefault("student_day_gap", 0)
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('objective_caps', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(caps),))
        conn.execute("DELETE FROM settings WHERE key = 'require_consecutive'")


@asynccontextmanager
async def _lifespan(app_: FastAPI):
    path = getattr(app_.state, "db_path", db.DEFAULT_DB_PATH)
    db.init_db(path)
    conn = db.connect(path)
    try:
        _migrate_settings(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="Cram School Scheduler", lifespan=_lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def get_conn():
    path = getattr(app.state, "db_path", db.DEFAULT_DB_PATH)
    conn = db.connect(path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- entities

class Named(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class RoomIn(Named):
    capacity: int = Field(default=1, ge=1)
    # max distinct teachers in the room per timeslot; 0 = no limit
    teacher_capacity: int = Field(default=0, ge=0)


class TimeslotIn(BaseModel):
    id: str = Field(min_length=1)
    date: str          # ISO YYYY-MM-DD
    period: int = Field(ge=1)
    label: str = ""


class LinkIn(BaseModel):
    pass


SIMPLE_TABLES = {"students", "subjects"}


def _rows(conn, sql, *params):
    return [dict(r) for r in conn.execute(sql, params)]


def _make_named_routes(table: str) -> None:
    def upsert(item: Named, conn: sqlite3.Connection = Depends(get_conn)):
        with conn:
            conn.execute(
                f"INSERT INTO {table} (id, name) VALUES (?, ?) "  # noqa: S608
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (item.id, item.name))
        return {"ok": True}

    def delete(item_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        with conn:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE id = ?", (item_id,))  # noqa: S608
        if cur.rowcount == 0:
            raise HTTPException(404, f"No such {table[:-1]} '{item_id}'")
        return {"ok": True}

    app.post(f"/api/{table}", name=f"upsert_{table}")(upsert)
    app.delete(f"/api/{table}/{{item_id}}", name=f"delete_{table}")(delete)


for _t in sorted(SIMPLE_TABLES):
    _make_named_routes(_t)


class TeacherIn(Named):
    # max lessons on one calendar day; 0 = no limit. None = leave the
    # stored value unchanged (so a rename never resets the limit).
    max_lessons_per_day: int | None = Field(default=None, ge=0)


@app.post("/api/teachers")
def upsert_teacher(item: TeacherIn,
                   conn: sqlite3.Connection = Depends(get_conn)):
    with conn:
        if item.max_lessons_per_day is None:
            conn.execute(
                "INSERT INTO teachers (id, name) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (item.id, item.name))
        else:
            conn.execute(
                "INSERT INTO teachers (id, name, max_lessons_per_day) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "max_lessons_per_day=excluded.max_lessons_per_day",
                (item.id, item.name, item.max_lessons_per_day))
    return {"ok": True}


@app.delete("/api/teachers/{item_id}")
def delete_teacher(item_id: str,
                   conn: sqlite3.Connection = Depends(get_conn)):
    with conn:
        cur = conn.execute("DELETE FROM teachers WHERE id = ?", (item_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, f"No such teacher '{item_id}'")
    return {"ok": True}


@app.post("/api/rooms")
def upsert_room(item: RoomIn, conn: sqlite3.Connection = Depends(get_conn)):
    with conn:
        conn.execute(
            "INSERT INTO rooms (id, name, capacity, teacher_capacity) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "capacity=excluded.capacity, "
            "teacher_capacity=excluded.teacher_capacity",
            (item.id, item.name, item.capacity, item.teacher_capacity))
    return {"ok": True}


@app.delete("/api/rooms/{item_id}")
def delete_room(item_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    with conn:
        cur = conn.execute("DELETE FROM rooms WHERE id = ?", (item_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, f"No such room '{item_id}'")
    return {"ok": True}


@app.post("/api/timeslots")
def upsert_timeslot(item: TimeslotIn, conn: sqlite3.Connection = Depends(get_conn)):
    if not csv_io._is_iso_date(item.date):
        raise HTTPException(422, "date must be YYYY-MM-DD (e.g. 2026-07-27)")
    try:
        with conn:
            conn.execute(
                "INSERT INTO timeslots (id, date, period, label) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET date=excluded.date, "
                "period=excluded.period, label=excluded.label",
                (item.id, item.date, item.period, item.label))
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"A timeslot for {item.date} period {item.period} already exists")
    return {"ok": True}


class BulkPeriodIn(BaseModel):
    period: int = Field(ge=1)
    label: str = ""


class BulkTimeslotsIn(BaseModel):
    start_date: str
    end_date: str
    weekdays: list[str]          # e.g. ["Mon", "Wed", "Sat"]
    periods: list[BulkPeriodIn]


@app.post("/api/timeslots/bulk")
def bulk_add_timeslots(body: BulkTimeslotsIn,
                       conn: sqlite3.Connection = Depends(get_conn)):
    """Create timeslots for every selected weekday in a date range.

    Existing (date, period) pairs are left untouched and counted as
    skipped. Ids are MMDD-period, falling back to YYYYMMDD-period if that
    id is already taken by a different date.
    """
    import datetime as dt
    for name, value in (("start_date", body.start_date),
                        ("end_date", body.end_date)):
        if not csv_io._is_iso_date(value):
            raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    start = dt.date.fromisoformat(body.start_date)
    end = dt.date.fromisoformat(body.end_date)
    if start > end:
        raise HTTPException(422, "start_date must not be after end_date")
    if (end - start).days > 400:
        raise HTTPException(422, "date range is longer than 400 days")
    bad_days = [d for d in body.weekdays if d not in views.WEEKDAYS]
    if bad_days:
        raise HTTPException(422, f"Unknown weekday(s): {', '.join(bad_days)}")
    if not body.weekdays:
        raise HTTPException(422, "Select at least one weekday")
    if not body.periods:
        raise HTTPException(422, "Define at least one period")
    period_nums = [p.period for p in body.periods]
    if len(set(period_nums)) != len(period_nums):
        raise HTTPException(422, "Duplicate period numbers")

    existing_pairs = {(r["date"], r["period"]) for r in
                      conn.execute("SELECT date, period FROM timeslots")}
    taken_ids = {r["id"] for r in conn.execute("SELECT id FROM timeslots")}
    rows, skipped = [], 0
    day = start
    while day <= end:
        if views.WEEKDAYS[day.weekday()] in body.weekdays:
            iso = day.isoformat()
            for p in body.periods:
                if (iso, p.period) in existing_pairs:
                    skipped += 1
                    continue
                sid = f"{day:%m%d}-{p.period}"
                if sid in taken_ids:
                    sid = f"{day:%Y%m%d}-{p.period}"
                taken_ids.add(sid)
                rows.append((sid, iso, p.period, p.label))
        day += dt.timedelta(days=1)
    with conn:
        conn.executemany(
            "INSERT INTO timeslots (id, date, period, label) VALUES (?, ?, ?, ?)",
            rows)
    return {"ok": True, "created": len(rows), "skipped": skipped}


@app.delete("/api/timeslots/{item_id}")
def delete_timeslot(item_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    with conn:
        cur = conn.execute("DELETE FROM timeslots WHERE id = ?", (item_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, f"No such timeslot '{item_id}'")
    return {"ok": True}


# ------------------------------------------------------------- link tables

class TeacherSubjectIn(BaseModel):
    teacher_id: str
    subject_id: str


class NeedIn(BaseModel):
    student_id: str
    subject_id: str
    sessions: int = Field(ge=1)   # total sessions over the whole term


class TeacherAvailIn(BaseModel):
    teacher_id: str
    timeslot_id: str


class StudentAvailIn(BaseModel):
    student_id: str
    timeslot_id: str


def _insert_link(conn, table: str, cols: list[str], values: tuple):
    try:
        with conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "  # noqa: S608
                f"VALUES ({','.join('?' for _ in cols)})", values)
    except sqlite3.IntegrityError as e:
        raise HTTPException(422, f"Unknown reference: {e}")


class TeacherStudentIn(BaseModel):
    teacher_id: str
    student_id: str
    # 0 = the student MUST be taught by this teacher; 1-9 = soft
    priority: int = Field(default=1, ge=0, le=9)


@app.post("/api/teacher_students")
def add_teacher_student(item: TeacherStudentIn, conn=Depends(get_conn)):
    prior = conn.execute(
        "SELECT priority FROM teacher_students WHERE teacher_id=? "
        "AND student_id=?", (item.teacher_id, item.student_id)).fetchone()
    _insert_link(conn, "teacher_students",
                 ["teacher_id", "student_id", "priority"],
                 (item.teacher_id, item.student_id, item.priority))
    if prior is None or prior["priority"] != item.priority:
        inv = ({"set": [[item.teacher_id, item.student_id,
                         prior["priority"]]], "clear": []}
               if prior is not None else
               {"set": [], "clear": [[item.teacher_id, item.student_id]]})
        _push_undo(conn, "assignment change", {"pairs": inv})
    return {"ok": True}


@app.delete("/api/teacher_students")
def del_teacher_student(teacher_id: str, student_id: str,
                        conn=Depends(get_conn)):
    prior = conn.execute(
        "SELECT priority FROM teacher_students WHERE teacher_id=? "
        "AND student_id=?", (teacher_id, student_id)).fetchone()
    with conn:
        conn.execute(
            "DELETE FROM teacher_students WHERE teacher_id=? AND student_id=?",
            (teacher_id, student_id))
    if prior is not None:
        _push_undo(conn, "assignment change", {"pairs": {
            "set": [[teacher_id, student_id, prior["priority"]]],
            "clear": []}})
    return {"ok": True}


@app.post("/api/teacher_subjects")
def add_teacher_subject(item: TeacherSubjectIn, conn=Depends(get_conn)):
    _insert_link(conn, "teacher_subjects", ["teacher_id", "subject_id"],
                 (item.teacher_id, item.subject_id))
    return {"ok": True}


@app.delete("/api/teacher_subjects")
def del_teacher_subject(teacher_id: str, subject_id: str, conn=Depends(get_conn)):
    with conn:
        conn.execute("DELETE FROM teacher_subjects WHERE teacher_id=? AND subject_id=?",
                     (teacher_id, subject_id))
    return {"ok": True}


@app.post("/api/student_needs")
def add_need(item: NeedIn, conn=Depends(get_conn)):
    _insert_link(conn, "student_needs",
                 ["student_id", "subject_id", "sessions"],
                 (item.student_id, item.subject_id, item.sessions))
    return {"ok": True}


@app.delete("/api/student_needs")
def del_need(student_id: str, subject_id: str, conn=Depends(get_conn)):
    with conn:
        conn.execute("DELETE FROM student_needs WHERE student_id=? AND subject_id=?",
                     (student_id, subject_id))
    return {"ok": True}


@app.post("/api/teacher_availability")
def add_teacher_avail(item: TeacherAvailIn, conn=Depends(get_conn)):
    existed = conn.execute(
        "SELECT 1 FROM teacher_availability WHERE teacher_id=? "
        "AND timeslot_id=?", (item.teacher_id, item.timeslot_id)).fetchone()
    _insert_link(conn, "teacher_availability", ["teacher_id", "timeslot_id"],
                 (item.teacher_id, item.timeslot_id))
    if not existed:
        _push_undo(conn, "availability change", {"avail": {
            "table": "teacher_availability", "add": [],
            "remove": [[item.teacher_id, item.timeslot_id]]}})
    return {"ok": True}


@app.delete("/api/teacher_availability")
def del_teacher_avail(teacher_id: str, timeslot_id: str, conn=Depends(get_conn)):
    with conn:
        cur = conn.execute(
            "DELETE FROM teacher_availability WHERE teacher_id=? AND timeslot_id=?",
            (teacher_id, timeslot_id))
    if cur.rowcount:
        _push_undo(conn, "availability change", {"avail": {
            "table": "teacher_availability",
            "add": [[teacher_id, timeslot_id]], "remove": []}})
    return {"ok": True}


@app.post("/api/student_availability")
def add_student_avail(item: StudentAvailIn, conn=Depends(get_conn)):
    existed = conn.execute(
        "SELECT 1 FROM student_availability WHERE student_id=? "
        "AND timeslot_id=?", (item.student_id, item.timeslot_id)).fetchone()
    _insert_link(conn, "student_availability", ["student_id", "timeslot_id"],
                 (item.student_id, item.timeslot_id))
    if not existed:
        _push_undo(conn, "availability change", {"avail": {
            "table": "student_availability", "add": [],
            "remove": [[item.student_id, item.timeslot_id]]}})
    return {"ok": True}


@app.delete("/api/student_availability")
def del_student_avail(student_id: str, timeslot_id: str, conn=Depends(get_conn)):
    with conn:
        cur = conn.execute(
            "DELETE FROM student_availability WHERE student_id=? AND timeslot_id=?",
            (student_id, timeslot_id))
    if cur.rowcount:
        _push_undo(conn, "availability change", {"avail": {
            "table": "student_availability",
            "add": [[student_id, timeslot_id]], "remove": []}})
    return {"ok": True}


# bulk grid edits (area select / paste in the Availability and
# Assignments tabs): one transaction instead of one request per cell

class AvailBulkIn(BaseModel):
    add: list[list[str]] = Field(default_factory=list)     # [person, slot]
    remove: list[list[str]] = Field(default_factory=list)


def _bulk_avail(conn, table: str, idcol: str, body: AvailBulkIn):
    if any(len(p) != 2 for p in body.add + body.remove):
        raise HTTPException(422, "pairs must be [person_id, timeslot_id]")
    existing = {(r[idcol], r["timeslot_id"]) for r in conn.execute(
        f"SELECT {idcol}, timeslot_id FROM {table}")}  # noqa: S608
    inverse = {"table": table,
               "add": [p for p in body.remove if tuple(p) in existing],
               "remove": [p for p in body.add if tuple(p) not in existing]}
    try:
        with conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} ({idcol}, timeslot_id) "  # noqa: S608
                "VALUES (?, ?)", [tuple(p) for p in body.add])
            conn.executemany(
                f"DELETE FROM {table} WHERE {idcol}=? AND timeslot_id=?",  # noqa: S608
                [tuple(p) for p in body.remove])
    except sqlite3.IntegrityError as e:
        raise HTTPException(422, f"Unknown reference: {e}")
    if inverse["add"] or inverse["remove"]:
        _push_undo(conn, "availability block edit", {"avail": inverse})
    return {"ok": True, "added": len(body.add),
            "removed": len(body.remove)}


@app.post("/api/teacher_availability/bulk")
def bulk_teacher_avail(body: AvailBulkIn, conn=Depends(get_conn)):
    return _bulk_avail(conn, "teacher_availability", "teacher_id", body)


@app.post("/api/student_availability/bulk")
def bulk_student_avail(body: AvailBulkIn, conn=Depends(get_conn)):
    return _bulk_avail(conn, "student_availability", "student_id", body)


class PairBulkIn(BaseModel):
    # [teacher_id, student_id, priority 0-9]
    set: list[list] = Field(default_factory=list)
    # [teacher_id, student_id]
    clear: list[list[str]] = Field(default_factory=list)


@app.post("/api/teacher_students/bulk")
def bulk_teacher_students(body: PairBulkIn, conn=Depends(get_conn)):
    for p in body.set:
        if (len(p) != 3 or not isinstance(p[2], int)
                or not 0 <= p[2] <= 9):
            raise HTTPException(
                422, "set entries must be [teacher_id, student_id, "
                     "priority 0-9]")
    if any(len(p) != 2 for p in body.clear):
        raise HTTPException(
            422, "clear entries must be [teacher_id, student_id]")
    prior = {(r["teacher_id"], r["student_id"]): r["priority"]
             for r in conn.execute(
                 "SELECT teacher_id, student_id, priority "
                 "FROM teacher_students")}
    inverse = {"set": [], "clear": []}
    for p in body.set:
        key = (p[0], p[1])
        if key in prior:
            if prior[key] != p[2]:
                inverse["set"].append([p[0], p[1], prior[key]])
        else:
            inverse["clear"].append([p[0], p[1]])
    for p in body.clear:
        key = (p[0], p[1])
        if key in prior:
            inverse["set"].append([p[0], p[1], prior[key]])
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO teacher_students "
                "(teacher_id, student_id, priority) VALUES (?, ?, ?)",
                [tuple(p) for p in body.set])
            conn.executemany(
                "DELETE FROM teacher_students "
                "WHERE teacher_id=? AND student_id=?",
                [tuple(p) for p in body.clear])
    except sqlite3.IntegrityError as e:
        raise HTTPException(422, f"Unknown reference: {e}")
    if inverse["set"] or inverse["clear"]:
        _push_undo(conn, "assignment block edit", {"pairs": inverse})
    return {"ok": True, "set": len(body.set), "cleared": len(body.clear)}


# ----------------------------------------------------------------- settings

# "student_day_gap" capped at 0 by default = the consecutiveness rule is
# always active out of the box; demote the card in the UI to relax it.
DEFAULT_SETTINGS = {"teacher_capacity": 2, "student_day_cap": 2,
                    "single_day_max": 1,
                    "objective_caps": {"student_day_gap": 0}}


class SettingsIn(BaseModel):
    teacher_capacity: int = Field(default=2, ge=1, le=4)
    student_day_cap: int = Field(default=2, ge=1, le=4)
    # the "teacher days with too few lessons" objective counts worked
    # days with at most this many lessons (1 = single-lesson days)
    single_day_max: int = Field(default=1, ge=1, le=10)
    # soft objectives promoted to hard constraints: term -> max value
    objective_caps: dict[str, int] = Field(default_factory=dict)


def get_settings(conn: sqlite3.Connection) -> dict:
    out = dict(DEFAULT_SETTINGS)
    for r in conn.execute("SELECT key, value FROM settings"):
        k, v = r["key"], r["value"]
        try:
            if k in ("teacher_capacity", "student_day_cap",
                     "single_day_max"):
                out[k] = int(v)
            elif k == "objective_caps":
                caps = json.loads(v)
                out[k] = {t: int(b) for t, b in caps.items()
                          if t in OBJECTIVE_TERMS}
        except (ValueError, TypeError):
            pass                    # corrupt row: keep the default
    return out


def _hard_consecutive(settings: dict) -> bool:
    return settings["objective_caps"].get("student_day_gap") == 0


@app.get("/api/settings")
def read_settings(conn: sqlite3.Connection = Depends(get_conn)):
    return get_settings(conn)


@app.put("/api/settings")
def write_settings(body: SettingsIn,
                   conn: sqlite3.Connection = Depends(get_conn)):
    bad_terms = [t for t in body.objective_caps if t not in OBJECTIVE_TERMS]
    if bad_terms:
        raise HTTPException(
            422, f"Unknown objective term(s): {', '.join(bad_terms)}")
    if any(b < 0 or b > 999 for b in body.objective_caps.values()):
        raise HTTPException(422, "objective cap bounds must be 0-999")
    rows = [("teacher_capacity", str(body.teacher_capacity)),
            ("student_day_cap", str(body.student_day_cap)),
            ("single_day_max", str(body.single_day_max)),
            ("objective_caps", json.dumps(body.objective_caps))]
    with conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", rows)
    return get_settings(conn)


def _validate_with_settings(conn, data, lessons):
    s = get_settings(conn)
    return validate(data, lessons, s["teacher_capacity"],
                    s["student_day_cap"], _hard_consecutive(s),
                    s["objective_caps"], single_day_max=s["single_day_max"])


# ------------------------------------------------------------ CSV import/export

@app.post("/api/import/{entity}")
async def import_entity(entity: str, file: UploadFile = File(...),
                        conn: sqlite3.Connection = Depends(get_conn)):
    if entity not in csv_io.SPECS:
        raise HTTPException(404, f"Unknown entity '{entity}'")
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "File must be UTF-8 encoded")
    try:
        n = csv_io.import_csv(conn, entity, text)
    except csv_io.CsvError as e:
        raise HTTPException(422, detail={"errors": e.errors})
    return {"ok": True, "rows": n}


@app.get("/api/export/{entity}")
def export_entity(entity: str, conn: sqlite3.Connection = Depends(get_conn)):
    if entity not in csv_io.SPECS:
        raise HTTPException(404, f"Unknown entity '{entity}'")
    return PlainTextResponse(
        csv_io.export_csv(conn, entity), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={entity}.csv"})


# ---------------------------------------------------------------- schedule

def load_dataset(conn: sqlite3.Connection) -> Dataset:
    data = Dataset()
    for r in conn.execute("SELECT id, name FROM students"):
        data.students[r["id"]] = r["name"]
    for r in conn.execute(
            "SELECT id, name, max_lessons_per_day FROM teachers"):
        data.teachers[r["id"]] = r["name"]
        if r["max_lessons_per_day"]:
            data.teacher_day_max[r["id"]] = r["max_lessons_per_day"]
    for r in conn.execute("SELECT id, name FROM subjects"):
        data.subjects[r["id"]] = r["name"]
    for r in conn.execute(
            "SELECT id, name, capacity, teacher_capacity FROM rooms"):
        data.rooms[r["id"]] = Room(r["id"], r["name"], r["capacity"],
                                   r["teacher_capacity"])
    for r in conn.execute("SELECT id, date, period, label FROM timeslots"):
        data.timeslots[r["id"]] = Timeslot(r["id"], r["date"], r["period"], r["label"])
    for r in conn.execute("SELECT teacher_id, subject_id FROM teacher_subjects"):
        data.teacher_subjects.add((r["teacher_id"], r["subject_id"]))
    for r in conn.execute(
            "SELECT teacher_id, student_id, priority FROM teacher_students"):
        data.teacher_students[(r["student_id"], r["teacher_id"])] = \
            r["priority"]
    for r in conn.execute(
            "SELECT student_id, subject_id, sessions FROM student_needs"):
        data.student_needs[(r["student_id"], r["subject_id"])] = r["sessions"]
    for r in conn.execute("SELECT teacher_id, timeslot_id FROM teacher_availability"):
        data.teacher_availability.add((r["teacher_id"], r["timeslot_id"]))
    for r in conn.execute("SELECT student_id, timeslot_id FROM student_availability"):
        data.student_availability.add((r["student_id"], r["timeslot_id"]))
    return data


def load_lessons(conn: sqlite3.Connection) -> list[Lesson]:
    return [Lesson(r["student_id"], r["subject_id"], r["teacher_id"],
                   r["room_id"], r["timeslot_id"], id=r["id"],
                   locked=bool(r["locked"]))
            for r in conn.execute(
                "SELECT id, student_id, subject_id, teacher_id, room_id, "
                "timeslot_id, locked FROM lessons ORDER BY id")]


class GenerateOptions(BaseModel):
    keep_existing: bool = False
    compress_teacher_days: bool = True
    solver: str = "v1"       # "v1" (backtracking + local search) or "v2"
    #                          (CP-SAT exact optimization, falls back to v1)
    # approximate search budget for the v2 solver, in seconds (it keeps
    # searching for the whole budget; runs are reproducible because the
    # cutoff is measured in CP-SAT's deterministic work units)
    v2_time_budget: float = Field(default=8.0, ge=1, le=600)
    # soft-objective priority, most important first; must be a
    # permutation of scheduler.OBJECTIVE_TERMS (None = default order)
    objective_order: list[str] | None = None


class LessonIn(BaseModel):
    student_id: str
    subject_id: str
    teacher_id: str
    room_id: str
    timeslot_id: str
    force: bool = False   # allow saving despite violations


UNDO_LIMIT = 30

# grid tables that support delta-undo: table -> its person id column
AVAIL_TABLES = {"teacher_availability": "teacher_id",
                "student_availability": "student_id"}


def _push_undo(conn: sqlite3.Connection, label: str,
               payload=None) -> None:
    """Record one undoable MANUAL edit. Without ``payload`` this
    snapshots the whole lessons table (restored verbatim, ids
    included); grid edits pass an INVERSE-delta payload instead:
    {"avail": {table, add, remove}} or {"pairs": {set, clear}} — the
    exact bulk operation that reverts the change."""
    if payload is None:
        payload = [dict(r) for r in conn.execute(
            "SELECT id, student_id, subject_id, teacher_id, room_id, "
            "timeslot_id, locked FROM lessons ORDER BY id")]
    with conn:
        conn.execute(
            "INSERT INTO undo_stack (label, snapshot) VALUES (?, ?)",
            (label, json.dumps(payload)))
        conn.execute(
            "DELETE FROM undo_stack WHERE id NOT IN "
            "(SELECT id FROM undo_stack ORDER BY id DESC LIMIT ?)",
            (UNDO_LIMIT,))


def _clear_undo(conn: sqlite3.Connection) -> None:
    """Solver runs and Clear schedule reset the manual-edit history —
    undo must never silently roll back a generated schedule."""
    with conn:
        conn.execute("DELETE FROM undo_stack")


def _undo_info(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT COUNT(*) AS n FROM undo_stack").fetchone()
    last = conn.execute("SELECT label FROM undo_stack "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    return {"count": row["n"], "label": last["label"] if last else None}


def _violations_json(vs):
    return [{"code": v.code, "message": v.message,
             "lesson_ids": [i for i in v.lesson_ids if i is not None]}
            for v in vs]


@app.post("/api/schedule/generate")
def generate_schedule(opts: GenerateOptions,
                      conn: sqlite3.Connection = Depends(get_conn)):
    if opts.solver not in ("v1", "v2"):
        raise HTTPException(422, "solver must be 'v1' or 'v2'")
    order = opts.objective_order
    if order is not None and sorted(order) != sorted(OBJECTIVE_TERMS):
        raise HTTPException(
            422, "objective_order must be a permutation of "
                 + ", ".join(OBJECTIVE_TERMS))
    data = load_dataset(conn)
    problems = check_input_problems(data)
    existing = load_lessons(conn)
    # user-locked lessons are ALWAYS pinned; "keep existing" pins all
    fixed = existing if opts.keep_existing else [
        l for l in existing if l.locked]
    s = get_settings(conn)
    if opts.solver == "v2":
        # exact CP-SAT optimization; validates its own output and falls
        # back to the v1 pipeline internally when it cannot do better
        # parallel wall-clock mode: the user's budget is real seconds.
        # A single deterministic worker spends its whole budget in
        # presolve on big terms and never even reaches the warm start;
        # the portfolio search optimizes hard within the same time.
        cfg = SolverConfig(
            teacher_capacity=s["teacher_capacity"],
            student_day_cap=s["student_day_cap"],
            require_consecutive=_hard_consecutive(s),
            single_day_max=s["single_day_max"],
            objective_caps=s["objective_caps"] or None,
            weights=ObjectiveWeights.lexicographic(order),
            num_workers=8,
            time_limit_seconds=opts.v2_time_budget)
        # the schedule that existed when the user clicked generate is
        # the bar to beat — a re-generate must never lose a good
        # (possibly hand-tuned or long-optimized) schedule
        result = solve_v2(data, config=cfg, fixed_lessons=fixed,
                          incumbent=existing)
    else:
        result = solve(data, fixed_lessons=fixed,
                       teacher_capacity=s["teacher_capacity"],
                       student_day_cap=s["student_day_cap"],
                       require_consecutive=_hard_consecutive(s))
        if opts.compress_teacher_days:
            # user-placed lessons carry a DB id and stay pinned; only
            # solver-generated ones (id None) may be rearranged.
            # promoted (capped) objectives lead the hill-climb order.
            capped = [t for t in (order or list(OBJECTIVE_TERMS))
                      if t in s["objective_caps"]]
            rest = [t for t in (order or list(OBJECTIVE_TERMS))
                    if t not in s["objective_caps"]]
            pinned = [l for l in result.lessons if l.id is not None]
            generated = [l for l in result.lessons if l.id is None]
            result.lessons = optimize_teacher_days(
                data, generated, fixed=pinned,
                teacher_capacity=s["teacher_capacity"],
                student_day_cap=s["student_day_cap"],
                require_consecutive=_hard_consecutive(s),
                objective_order=capped + rest,
                single_day_max=s["single_day_max"])
    _clear_undo(conn)
    with conn:
        conn.execute("DELETE FROM lessons")
        conn.executemany(
            "INSERT INTO lessons (student_id, subject_id, teacher_id, "
            "room_id, timeslot_id, locked) VALUES (?, ?, ?, ?, ?, ?)",
            [(l.student_id, l.subject_id, l.teacher_id, l.room_id,
              l.timeslot_id, int(l.locked))
             for l in result.lessons])
    return {
        "complete": result.complete,
        "scheduled": len(result.lessons),
        "backend": result.backend,
        "v2_outcome": result.v2_outcome,
        "unscheduled": [
            {"student_id": st, "subject_id": su, "missing": n}
            for (st, su, n) in result.unscheduled],
        "input_problems": problems,
    }


@app.get("/api/schedule")
def get_schedule(conn: sqlite3.Connection = Depends(get_conn)):
    data = load_dataset(conn)
    lessons = load_lessons(conn)
    stats = teacher_day_stats(data, lessons)
    sstats = student_day_stats(data, lessons)
    s = get_settings(conn)
    (double_days, gap_days, pair_miss, slot_spread, total_days,
     single_days, day_spread) = schedule_objective(
        data, lessons, single_day_max=s["single_day_max"])
    return {
        "lessons": [l.__dict__ for l in lessons],
        "violations": _violations_json(
            _validate_with_settings(conn, data, lessons)),
        "coverage": _violations_json(coverage_report(data, lessons)),
        "teacher_stats": [
            {"teacher_id": t, "name": data.teachers[t],
             "lessons": stats[t]["lessons"],
             "days": len(stats[t]["days"])}
            for t in sorted(data.teachers, key=lambda t: data.teachers[t])],
        "student_stats": [
            {"student_id": st, "name": data.students[st], **sstats[st]}
            for st in sorted(data.students, key=lambda st: data.students[st])],
        "objective": {"student_double_days": double_days,
                      "student_day_gaps": gap_days,
                      "pair_miss": pair_miss,
                      "slot_spread": slot_spread, "total_days": total_days,
                      "teacher_single_days": single_days,
                      "day_spread": day_spread},
        "undo": _undo_info(conn),
    }


@app.post("/api/lessons")
def add_lesson(item: LessonIn, conn: sqlite3.Connection = Depends(get_conn)):
    data = load_dataset(conn)
    candidate = Lesson(item.student_id, item.subject_id, item.teacher_id,
                       item.room_id, item.timeslot_id)
    lessons = load_lessons(conn) + [candidate]
    new_violations = [
        v for v in _validate_with_settings(conn, data, lessons)
        if None in v.lesson_ids]  # violations involving the new lesson
    if new_violations and not item.force:
        raise HTTPException(409, detail={"violations": _violations_json(new_violations)})
    _push_undo(conn, "add lesson")
    with conn:
        cur = conn.execute(
            "INSERT INTO lessons (student_id, subject_id, teacher_id, room_id, timeslot_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (item.student_id, item.subject_id, item.teacher_id,
             item.room_id, item.timeslot_id))
    return {"ok": True, "id": cur.lastrowid,
            "violations": _violations_json(new_violations)}


class RepeatIn(BaseModel):
    lesson_ids: list[int] = Field(min_length=1)
    weeks: int = Field(ge=1, le=12)     # how many following weeks
    force: bool = False                 # save despite violations


@app.post("/api/lessons/repeat")
def repeat_lessons(body: RepeatIn,
                   conn: sqlite3.Connection = Depends(get_conn)):
    """Copy the given lessons onto the same weekday and period of the
    following N weeks (student/subject/teacher/room unchanged). Weeks
    with no matching (date, period) timeslot are skipped, as are exact
    duplicates; conflicts go through the usual 409-unless-force flow."""
    data = load_dataset(conn)
    lessons = load_lessons(conn)
    by_id = {l.id: l for l in lessons}
    missing = [i for i in body.lesson_ids if i not in by_id]
    if missing:
        raise HTTPException(
            404, f"No such lesson(s): {', '.join(map(str, missing))}")

    slot_at = {(s.date, s.period): s.id for s in data.timeslots.values()}
    seen = {(l.student_id, l.subject_id, l.teacher_id, l.room_id,
             l.timeslot_id) for l in lessons}
    new: list[Lesson] = []
    skipped_no_slot = 0
    skipped_duplicate = 0
    for lid in body.lesson_ids:
        src = by_id[lid]
        slot = data.timeslots[src.timeslot_id]
        base = datetime.date.fromisoformat(slot.date)
        for k in range(1, body.weeks + 1):
            target = (base + datetime.timedelta(weeks=k)).isoformat()
            sid = slot_at.get((target, slot.period))
            if sid is None:
                skipped_no_slot += 1
                continue
            key = (src.student_id, src.subject_id, src.teacher_id,
                   src.room_id, sid)
            if key in seen:
                skipped_duplicate += 1
                continue
            seen.add(key)
            new.append(Lesson(*key))

    new_violations = []
    if new:
        new_violations = [
            v for v in _validate_with_settings(conn, data, lessons + new)
            if None in v.lesson_ids]   # violations involving new lessons
        if new_violations and not body.force:
            raise HTTPException(409, detail={
                "violations": _violations_json(new_violations)})
        _push_undo(conn, "repeat lessons")
        with conn:
            conn.executemany(
                "INSERT INTO lessons (student_id, subject_id, teacher_id, "
                "room_id, timeslot_id) VALUES (?, ?, ?, ?, ?)",
                [(l.student_id, l.subject_id, l.teacher_id, l.room_id,
                  l.timeslot_id) for l in new])
    return {"ok": True, "created": len(new),
            "skipped_no_slot": skipped_no_slot,
            "skipped_duplicate": skipped_duplicate,
            "violations": _violations_json(new_violations)}


class BulkUpdateIn(BaseModel):
    lesson_ids: list[int] = Field(min_length=1)
    # only the provided fields change; omitted ones are kept per lesson
    subject_id: str | None = None
    teacher_id: str | None = None
    room_id: str | None = None
    force: bool = False


@app.post("/api/lessons/bulk_update")
def bulk_update_lessons(body: BulkUpdateIn,
                        conn: sqlite3.Connection = Depends(get_conn)):
    """Change the subject/teacher/room of several lessons at once.
    Locked lessons are skipped (reported); the combined result is
    validated like any edit — 409 with the violations unless force."""
    changes = {k: v for k, v in [("subject_id", body.subject_id),
                                 ("teacher_id", body.teacher_id),
                                 ("room_id", body.room_id)]
               if v is not None}
    if not changes:
        raise HTTPException(
            422, "Nothing to change — provide subject_id, teacher_id "
                 "and/or room_id")
    data = load_dataset(conn)
    for key, pool in [("subject_id", data.subjects),
                      ("teacher_id", data.teachers),
                      ("room_id", data.rooms)]:
        if key in changes and changes[key] not in pool:
            raise HTTPException(422, f"Unknown {key} '{changes[key]}'")

    lessons = load_lessons(conn)
    by_id = {l.id: l for l in lessons}
    missing = [i for i in body.lesson_ids if i not in by_id]
    if missing:
        raise HTTPException(
            404, f"No such lesson(s): {', '.join(map(str, missing))}")
    targets = [by_id[i] for i in body.lesson_ids]
    todo = [l for l in targets if not l.locked]
    skipped_locked = len(targets) - len(todo)

    new_violations = []
    if todo:
        updated = [Lesson(l.student_id,
                          changes.get("subject_id", l.subject_id),
                          changes.get("teacher_id", l.teacher_id),
                          changes.get("room_id", l.room_id),
                          l.timeslot_id, id=l.id) for l in todo]
        changed_ids = {l.id for l in todo}
        proposed = [l for l in lessons
                    if l.id not in changed_ids] + updated
        new_violations = [
            v for v in _validate_with_settings(conn, data, proposed)
            if changed_ids & set(v.lesson_ids)]
        if new_violations and not body.force:
            raise HTTPException(409, detail={
                "violations": _violations_json(new_violations)})
        _push_undo(conn, "edit selected lessons")
        with conn:
            conn.executemany(
                "UPDATE lessons SET subject_id=?, teacher_id=?, room_id=? "
                "WHERE id=?",
                [(l.subject_id, l.teacher_id, l.room_id, l.id)
                 for l in updated])
    return {"ok": True, "updated": len(todo),
            "skipped_locked": skipped_locked,
            "violations": _violations_json(new_violations)}


class LessonPatch(BaseModel):
    student_id: str | None = None
    subject_id: str | None = None
    teacher_id: str | None = None
    room_id: str | None = None
    timeslot_id: str | None = None
    force: bool = False


@app.patch("/api/lessons/{lesson_id}")
def update_lesson(lesson_id: int, patch: LessonPatch,
                  conn: sqlite3.Connection = Depends(get_conn)):
    """Move/edit a lesson (e.g. drag to another timeslot), validating the
    result the same way as adding a lesson."""
    lessons = load_lessons(conn)
    target = next((l for l in lessons if l.id == lesson_id), None)
    if target is None:
        raise HTTPException(404, f"No such lesson {lesson_id}")
    if target.locked:
        raise HTTPException(
            409, "This lesson is locked — unlock it before moving or "
                 "editing it")

    data = load_dataset(conn)
    fields = {k: v for k, v in patch.model_dump().items()
              if k != "force" and v is not None}
    # Unknown references can never be saved (FK constraint) — flat 422.
    for key, pool in [("student_id", data.students),
                      ("subject_id", data.subjects),
                      ("teacher_id", data.teachers),
                      ("room_id", data.rooms),
                      ("timeslot_id", data.timeslots)]:
        if key in fields and fields[key] not in pool:
            raise HTTPException(422, f"Unknown {key} '{fields[key]}'")

    updated = Lesson(
        fields.get("student_id", target.student_id),
        fields.get("subject_id", target.subject_id),
        fields.get("teacher_id", target.teacher_id),
        fields.get("room_id", target.room_id),
        fields.get("timeslot_id", target.timeslot_id),
        id=lesson_id)
    new_schedule = [l for l in lessons if l.id != lesson_id] + [updated]
    new_violations = [
        v for v in _validate_with_settings(conn, data, new_schedule)
        if lesson_id in v.lesson_ids]
    if new_violations and not patch.force:
        raise HTTPException(409, detail={"violations": _violations_json(new_violations)})
    _push_undo(conn, "move/edit lesson")
    with conn:
        conn.execute(
            "UPDATE lessons SET student_id=?, subject_id=?, teacher_id=?, "
            "room_id=?, timeslot_id=? WHERE id=?",
            (updated.student_id, updated.subject_id, updated.teacher_id,
             updated.room_id, updated.timeslot_id, lesson_id))
    return {"ok": True, "lesson": updated.__dict__,
            "violations": _violations_json(new_violations)}


class OptionsIn(BaseModel):
    """Proposed (partial) edit; omitted fields default to the lesson's
    current values."""
    subject_id: str | None = None
    teacher_id: str | None = None
    room_id: str | None = None


@app.post("/api/lessons/{lesson_id}/check_options")
def check_lesson_options(lesson_id: int, opts: OptionsIn,
                         conn: sqlite3.Connection = Depends(get_conn)):
    """Dry-run validation for the inline editor.

    Returns the constraint problems of the proposed combination, plus —
    for each field — the problems every alternative option would cause
    (substituted into the proposal with the other two fields held fixed).
    Nothing is written; the real validator is the single source of truth.
    """
    lessons = load_lessons(conn)
    target = next((l for l in lessons if l.id == lesson_id), None)
    if target is None:
        raise HTTPException(404, f"No such lesson {lesson_id}")

    data = load_dataset(conn)
    others = [l for l in lessons if l.id != lesson_id]
    su0 = opts.subject_id or target.subject_id
    t0 = opts.teacher_id or target.teacher_id
    r0 = opts.room_id or target.room_id

    def problems(su: str, t: str, r: str) -> list[str]:
        cand = Lesson(target.student_id, su, t, r,
                      target.timeslot_id, id=lesson_id)
        return [v.message
                for v in _validate_with_settings(conn, data, others + [cand])
                if lesson_id in v.lesson_ids]

    return {
        "current": problems(su0, t0, r0),
        "subjects": {su: problems(su, t0, r0) for su in sorted(data.subjects)},
        "teachers": {t: problems(su0, t, r0) for t in sorted(data.teachers)},
        "rooms": {r: problems(su0, t0, r) for r in sorted(data.rooms)},
    }


class LockIn(BaseModel):
    locked: bool


@app.post("/api/lessons/{lesson_id}/lock")
def set_lesson_lock(lesson_id: int, body: LockIn,
                    conn: sqlite3.Connection = Depends(get_conn)):
    """Lock/unlock a lesson in place: locked lessons are always pinned
    at generate time, survive Clear schedule, and refuse moves, edits
    and deletion until unlocked."""
    with conn:
        cur = conn.execute("UPDATE lessons SET locked = ? WHERE id = ?",
                           (int(body.locked), lesson_id))
    if cur.rowcount == 0:
        raise HTTPException(404, f"No such lesson {lesson_id}")
    return {"ok": True, "locked": body.locked}


@app.delete("/api/lessons/{lesson_id}")
def delete_lesson(lesson_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT locked FROM lessons WHERE id = ?",
                       (lesson_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"No such lesson {lesson_id}")
    if row["locked"]:
        raise HTTPException(
            409, "This lesson is locked — unlock it before deleting it")
    _push_undo(conn, "delete lesson")
    with conn:
        conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))
    return {"ok": True}


@app.delete("/api/schedule")
def clear_schedule(conn: sqlite3.Connection = Depends(get_conn)):
    """Clear the schedule. Locked lessons survive — unlock them (or
    delete them individually) to remove them."""
    _clear_undo(conn)
    with conn:
        cur = conn.execute("DELETE FROM lessons WHERE locked = 0")
    kept = conn.execute(
        "SELECT COUNT(*) AS n FROM lessons").fetchone()["n"]
    return {"ok": True, "deleted": cur.rowcount, "kept_locked": kept}


@app.get("/api/undo")
def undo_info(conn: sqlite3.Connection = Depends(get_conn)):
    return _undo_info(conn)


@app.post("/api/schedule/undo")
def undo_last_edit(conn: sqlite3.Connection = Depends(get_conn)):
    """Revert the last MANUAL edit: timetable changes (add / move /
    edit / bulk edit / repeat / delete — restored from a full lessons
    snapshot) and Availability / Assignments grid edits (restored by
    applying the stored inverse delta). Solver runs and Clear schedule
    reset the history, so undo never rolls back a generated
    schedule."""
    row = conn.execute("SELECT id, label, snapshot FROM undo_stack "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        raise HTTPException(404, "Nothing to undo")
    snap = json.loads(row["snapshot"])
    try:
        with conn:
            conn.execute("DELETE FROM undo_stack WHERE id = ?",
                         (row["id"],))
            if isinstance(snap, list):
                # lessons snapshot: restore the table verbatim
                conn.execute("DELETE FROM lessons")
                conn.executemany(
                    "INSERT INTO lessons (id, student_id, subject_id, "
                    "teacher_id, room_id, timeslot_id, locked) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(r["id"], r["student_id"], r["subject_id"],
                      r["teacher_id"], r["room_id"], r["timeslot_id"],
                      r["locked"]) for r in snap])
            elif "avail" in snap:
                d = snap["avail"]
                idcol = AVAIL_TABLES.get(d.get("table"))
                if idcol is None:
                    raise sqlite3.IntegrityError("unknown table")
                conn.executemany(
                    f"INSERT OR REPLACE INTO {d['table']} "  # noqa: S608
                    f"({idcol}, timeslot_id) VALUES (?, ?)",
                    [tuple(p) for p in d["add"]])
                conn.executemany(
                    f"DELETE FROM {d['table']} "  # noqa: S608
                    f"WHERE {idcol}=? AND timeslot_id=?",
                    [tuple(p) for p in d["remove"]])
            elif "pairs" in snap:
                d = snap["pairs"]
                conn.executemany(
                    "INSERT OR REPLACE INTO teacher_students "
                    "(teacher_id, student_id, priority) VALUES (?, ?, ?)",
                    [tuple(p) for p in d["set"]])
                conn.executemany(
                    "DELETE FROM teacher_students "
                    "WHERE teacher_id=? AND student_id=?",
                    [tuple(p) for p in d["clear"]])
            else:
                raise sqlite3.IntegrityError("unknown snapshot format")
    except sqlite3.IntegrityError:
        # the snapshot references data that no longer exists (e.g. a
        # deleted student) — drop the stale entry instead of failing
        # forever
        with conn:
            conn.execute("DELETE FROM undo_stack WHERE id = ?",
                         (row["id"],))
        raise HTTPException(
            409, "Cannot undo: students/teachers/rooms/timeslots have "
                 "changed since that edit")
    left = conn.execute(
        "SELECT COUNT(*) AS n FROM undo_stack").fetchone()["n"]
    return {"ok": True, "undid": row["label"], "remaining": left}


@app.get("/api/schedule/check")
def check_inputs(conn: sqlite3.Connection = Depends(get_conn)):
    return {"problems": check_input_problems(load_dataset(conn))}


# ------------------------------------------------------------ backup/restore

@app.get("/api/backup.db")
def download_backup(conn: sqlite3.Connection = Depends(get_conn)):
    """A consistent snapshot of the whole database (SQLite backup API,
    safe while the app is in use), timestamped for easy versioning."""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dest = sqlite3.connect(tmp)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        data = Path(tmp).read_bytes()
    finally:
        os.unlink(tmp)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=data, media_type="application/x-sqlite3",
        headers={"Content-Disposition":
                 f'attachment; filename="school-backup-{stamp}.db"'})


@app.post("/api/backup.db")
async def restore_backup(file: UploadFile = File(...)):
    """Replace the ENTIRE database with an uploaded backup. The file is
    validated first (SQLite format, integrity, expected tables) and the
    schema migrations re-run afterwards, so older backups restore
    cleanly. All current data is lost — the UI double-confirms."""
    raw = await file.read()
    if not raw.startswith(b"SQLite format 3\x00"):
        raise HTTPException(422, "Not an SQLite database file")
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".db")
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    try:
        check = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            check.close()
        required = {"students", "teachers", "subjects", "rooms",
                    "timeslots", "lessons", "student_needs"}
        if ok != "ok":
            raise HTTPException(422, "The database file is corrupted")
        if not required <= tables:
            raise HTTPException(
                422, "Not a scheduler database (missing tables: "
                     + ", ".join(sorted(required - tables)) + ")")
        path = Path(getattr(app.state, "db_path", db.DEFAULT_DB_PATH))
        os.replace(tmp, path)
        tmp = None                     # consumed by the replace
        for side in (f"{path}-wal", f"{path}-shm"):
            Path(side).unlink(missing_ok=True)
    finally:
        if tmp:
            os.unlink(tmp)
    db.init_db(path)                   # migrate older backups in place
    conn = db.connect(path)
    try:
        _migrate_settings(conn)
        counts = {t: conn.execute(
            f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]  # noqa: S608
            for t in ("students", "teachers", "lessons")}
    finally:
        conn.close()
    return {"ok": True, **counts}


# ------------------------------------------------------------ calendar views

@app.get("/api/views/overview")
def view_overview(conn: sqlite3.Connection = Depends(get_conn)):
    return views.build_overview(load_dataset(conn), load_lessons(conn))


@app.get("/api/views/student/{student_id}")
def view_student(student_id: str,
                 conn: sqlite3.Connection = Depends(get_conn)):
    try:
        return views.build_student_view(
            load_dataset(conn), load_lessons(conn), student_id)
    except KeyError:
        raise HTTPException(404, f"No such student '{student_id}'")


@app.get("/api/views/teacher/{teacher_id}")
def view_teacher(teacher_id: str,
                 conn: sqlite3.Connection = Depends(get_conn)):
    try:
        return views.build_teacher_view(
            load_dataset(conn), load_lessons(conn), teacher_id)
    except KeyError:
        raise HTTPException(404, f"No such teacher '{teacher_id}'")


# Generic listing endpoint — registered LAST so fixed paths like
# /api/schedule are matched first (Starlette matches in registration order).
@app.get("/api/{entity}")
def list_entity(entity: str, conn: sqlite3.Connection = Depends(get_conn)):
    if entity not in csv_io.SPECS:
        raise HTTPException(404, f"Unknown entity '{entity}'")
    cols = ",".join(csv_io.SPECS[entity])
    return _rows(conn, f"SELECT {cols} FROM {entity} ORDER BY {cols}")  # noqa: S608


# ---------------------------------------------------------------- static UI

class NoCacheStaticFiles(StaticFiles):
    """Static files with `Cache-Control: no-cache`.

    Browsers must revalidate before reusing a cached copy, so UI updates
    show up on a normal refresh instead of requiring a hard refresh.
    (ETags still make unchanged files cheap 304s.)
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
