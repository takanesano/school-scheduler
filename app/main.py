"""FastAPI application: REST API + static web UI.

Run locally with:  .venv/bin/uvicorn app.main:app --reload
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
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


class TimeslotIn(BaseModel):
    id: str = Field(min_length=1)
    date: str          # ISO YYYY-MM-DD
    period: int = Field(ge=1)
    label: str = ""


class LinkIn(BaseModel):
    pass


SIMPLE_TABLES = {"students", "teachers", "subjects"}


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


@app.post("/api/rooms")
def upsert_room(item: RoomIn, conn: sqlite3.Connection = Depends(get_conn)):
    with conn:
        conn.execute(
            "INSERT INTO rooms (id, name, capacity) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, capacity=excluded.capacity",
            (item.id, item.name, item.capacity))
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
    _insert_link(conn, "teacher_availability", ["teacher_id", "timeslot_id"],
                 (item.teacher_id, item.timeslot_id))
    return {"ok": True}


@app.delete("/api/teacher_availability")
def del_teacher_avail(teacher_id: str, timeslot_id: str, conn=Depends(get_conn)):
    with conn:
        conn.execute(
            "DELETE FROM teacher_availability WHERE teacher_id=? AND timeslot_id=?",
            (teacher_id, timeslot_id))
    return {"ok": True}


@app.post("/api/student_availability")
def add_student_avail(item: StudentAvailIn, conn=Depends(get_conn)):
    _insert_link(conn, "student_availability", ["student_id", "timeslot_id"],
                 (item.student_id, item.timeslot_id))
    return {"ok": True}


@app.delete("/api/student_availability")
def del_student_avail(student_id: str, timeslot_id: str, conn=Depends(get_conn)):
    with conn:
        conn.execute(
            "DELETE FROM student_availability WHERE student_id=? AND timeslot_id=?",
            (student_id, timeslot_id))
    return {"ok": True}


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
    for r in conn.execute("SELECT id, name FROM teachers"):
        data.teachers[r["id"]] = r["name"]
    for r in conn.execute("SELECT id, name FROM subjects"):
        data.subjects[r["id"]] = r["name"]
    for r in conn.execute("SELECT id, name, capacity FROM rooms"):
        data.rooms[r["id"]] = Room(r["id"], r["name"], r["capacity"])
    for r in conn.execute("SELECT id, date, period, label FROM timeslots"):
        data.timeslots[r["id"]] = Timeslot(r["id"], r["date"], r["period"], r["label"])
    for r in conn.execute("SELECT teacher_id, subject_id FROM teacher_subjects"):
        data.teacher_subjects.add((r["teacher_id"], r["subject_id"]))
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
                   r["room_id"], r["timeslot_id"], id=r["id"])
            for r in conn.execute(
                "SELECT id, student_id, subject_id, teacher_id, room_id, timeslot_id "
                "FROM lessons ORDER BY id")]


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
    fixed = load_lessons(conn) if opts.keep_existing else []
    s = get_settings(conn)
    if opts.solver == "v2":
        # exact CP-SAT optimization; validates its own output and falls
        # back to the v1 pipeline internally when it cannot do better
        cfg = SolverConfig(
            teacher_capacity=s["teacher_capacity"],
            student_day_cap=s["student_day_cap"],
            require_consecutive=_hard_consecutive(s),
            single_day_max=s["single_day_max"],
            objective_caps=s["objective_caps"] or None,
            weights=ObjectiveWeights.lexicographic(order),
            deterministic_time=opts.v2_time_budget,
            time_limit_seconds=opts.v2_time_budget * 3 + 10)  # wall safety
        result = solve_v2(data, config=cfg, fixed_lessons=fixed)
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
    with conn:
        conn.execute("DELETE FROM lessons")
        conn.executemany(
            "INSERT INTO lessons (student_id, subject_id, teacher_id, room_id, timeslot_id) "
            "VALUES (?, ?, ?, ?, ?)",
            [(l.student_id, l.subject_id, l.teacher_id, l.room_id, l.timeslot_id)
             for l in result.lessons])
    return {
        "complete": result.complete,
        "scheduled": len(result.lessons),
        "backend": result.backend,
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
    (double_days, gap_days, slot_spread, total_days, single_days,
     day_spread) = schedule_objective(
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
                      "slot_spread": slot_spread, "total_days": total_days,
                      "teacher_single_days": single_days,
                      "day_spread": day_spread},
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
    with conn:
        cur = conn.execute(
            "INSERT INTO lessons (student_id, subject_id, teacher_id, room_id, timeslot_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (item.student_id, item.subject_id, item.teacher_id,
             item.room_id, item.timeslot_id))
    return {"ok": True, "id": cur.lastrowid,
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


@app.delete("/api/lessons/{lesson_id}")
def delete_lesson(lesson_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    with conn:
        cur = conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, f"No such lesson {lesson_id}")
    return {"ok": True}


@app.delete("/api/schedule")
def clear_schedule(conn: sqlite3.Connection = Depends(get_conn)):
    with conn:
        conn.execute("DELETE FROM lessons")
    return {"ok": True}


@app.get("/api/schedule/check")
def check_inputs(conn: sqlite3.Connection = Depends(get_conn)):
    return {"problems": check_input_problems(load_dataset(conn))}


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
