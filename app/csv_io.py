"""CSV import/export for every entity.

Import is all-or-nothing per file: the whole CSV is parsed and validated
first; only if every row is valid does it replace that table's contents.
Errors carry 1-based line numbers (header = line 1).
"""
from __future__ import annotations

import csv
import datetime
import io
import sqlite3


class CsvError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# entity -> (columns, required columns)
SPECS: dict[str, list[str]] = {
    "students": ["id", "name"],
    "teachers": ["id", "name", "max_lessons_per_day"],
    "subjects": ["id", "name"],
    "rooms": ["id", "name", "capacity", "teacher_capacity"],
    "timeslots": ["id", "date", "period", "label"],
    "teacher_subjects": ["teacher_id", "subject_id"],
    "student_needs": ["student_id", "subject_id", "sessions"],
    "teacher_availability": ["teacher_id", "timeslot_id"],
    "student_availability": ["student_id", "timeslot_id"],
}

OPTIONAL: dict[str, set[str]] = {
    # capacity defaults to 1; teacher_capacity to 0 = no teacher limit
    "rooms": {"capacity", "teacher_capacity"},
    "teachers": {"max_lessons_per_day"},   # 0 = no daily limit
    "timeslots": {"label"},     # defaults to ''
}

# columns that must reference another table: col -> referenced entity
FOREIGN = {
    "teacher_id": "teachers",
    "subject_id": "subjects",
    "student_id": "students",
    "timeslot_id": "timeslots",
}


def parse_csv(entity: str, text: str) -> list[dict[str, str]]:
    """Parse and validate one CSV file; raise CsvError with all problems."""
    if entity not in SPECS:
        raise CsvError([f"Unknown entity '{entity}'"])
    cols = SPECS[entity]
    optional = OPTIONAL.get(entity, set())

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None:
        raise CsvError(["File is empty"])
    fieldnames = [f.strip() for f in reader.fieldnames]
    missing_cols = [c for c in cols if c not in fieldnames and c not in optional]
    if missing_cols:
        raise CsvError(
            [f"Missing column(s): {', '.join(missing_cols)} "
             f"(expected header: {','.join(cols)})"])

    errors: list[str] = []
    rows: list[dict[str, str]] = []
    seen_keys: set[tuple] = set()
    key_cols = _key_columns(entity)

    for lineno, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip()
               for k, v in raw.items() if k is not None}
        out: dict[str, str] = {}
        row_ok = True
        for c in cols:
            val = row.get(c, "")
            if not val and c in optional:
                val = {"capacity": "1", "label": "",
                       "teacher_capacity": "0",
                       "max_lessons_per_day": "0"}[c]
            if not val and c not in optional:
                errors.append(f"Line {lineno}: '{c}' is empty")
                row_ok = False
                continue
            out[c] = val
        if not row_ok:
            continue

        if "capacity" in out and not _is_pos_int(out["capacity"]):
            errors.append(f"Line {lineno}: capacity must be a positive "
                          f"integer, got '{out['capacity']}'")
            continue
        if ("teacher_capacity" in out
                and not _is_nonneg_int(out["teacher_capacity"])):
            errors.append(f"Line {lineno}: teacher_capacity must be a "
                          f"non-negative integer (0 = no limit), got "
                          f"'{out['teacher_capacity']}'")
            continue
        if ("max_lessons_per_day" in out
                and not _is_nonneg_int(out["max_lessons_per_day"])):
            errors.append(f"Line {lineno}: max_lessons_per_day must be a "
                          f"non-negative integer (0 = no limit), got "
                          f"'{out['max_lessons_per_day']}'")
            continue
        if "period" in out and not _is_pos_int(out["period"]):
            errors.append(f"Line {lineno}: period must be a positive "
                          f"integer, got '{out['period']}'")
            continue
        if "sessions" in out and not _is_pos_int(out["sessions"]):
            errors.append(f"Line {lineno}: sessions must be a "
                          f"positive integer, got '{out['sessions']}'")
            continue
        if "date" in out and not _is_iso_date(out["date"]):
            errors.append(f"Line {lineno}: date must be YYYY-MM-DD "
                          f"(e.g. 2026-07-27), got '{out['date']}'")
            continue

        key = tuple(out[c] for c in key_cols)
        if key in seen_keys:
            errors.append(f"Line {lineno}: duplicate entry "
                          f"{dict(zip(key_cols, key))}")
            continue
        seen_keys.add(key)
        rows.append(out)

    if errors:
        raise CsvError(errors)
    return rows


def _key_columns(entity: str) -> list[str]:
    if "id" in SPECS[entity]:
        return ["id"]
    return [c for c in SPECS[entity] if c.endswith("_id")]


def _is_pos_int(v: str) -> bool:
    try:
        return int(v) >= 1
    except ValueError:
        return False


def _is_nonneg_int(v: str) -> bool:
    try:
        return int(v) >= 0
    except ValueError:
        return False


def _is_iso_date(v: str) -> bool:
    try:
        return datetime.date.fromisoformat(v).isoformat() == v
    except ValueError:
        return False


def import_csv(conn: sqlite3.Connection, entity: str, text: str) -> int:
    """Validate and atomically replace the table's contents. Returns row count."""
    rows = parse_csv(entity, text)

    # Referential checks against data already in the DB (link tables only).
    errors: list[str] = []
    for col, ref in FOREIGN.items():
        if col not in SPECS[entity]:
            continue
        existing = {r["id"] for r in
                    conn.execute(f"SELECT id FROM {ref}")}  # noqa: S608
        for row in rows:
            if row[col] not in existing:
                errors.append(
                    f"{col} '{row[col]}' does not exist in {ref} — "
                    f"import {ref}.csv first")
    if errors:
        raise CsvError(sorted(set(errors)))

    cols = SPECS[entity]
    key_cols = _key_columns(entity)
    placeholders = ",".join("?" for _ in cols)
    non_key = [c for c in cols if c not in key_cols]
    with conn:  # one transaction: the file becomes the table's full contents
        if key_cols == ["id"]:
            # Upsert then prune, so unchanged rows keep their identity and
            # dependent rows (availability, lessons, …) are not cascaded away.
            updates = ",".join(f"{c}=excluded.{c}" for c in non_key)
            conn.executemany(
                f"INSERT INTO {entity} ({','.join(cols)}) VALUES ({placeholders}) "  # noqa: S608
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                [tuple(r[c] for c in cols) for r in rows])
            keep = [r["id"] for r in rows]
            qs = ",".join("?" for _ in keep) or "''"
            conn.execute(
                f"DELETE FROM {entity} WHERE id NOT IN ({qs})", keep)  # noqa: S608
        else:
            # Pure link tables: nothing references them, replace wholesale.
            conn.execute(f"DELETE FROM {entity}")  # noqa: S608
            conn.executemany(
                f"INSERT INTO {entity} ({','.join(cols)}) VALUES ({placeholders})",  # noqa: S608
                [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def export_csv(conn: sqlite3.Connection, entity: str) -> str:
    if entity not in SPECS:
        raise CsvError([f"Unknown entity '{entity}'"])
    cols = SPECS[entity]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(cols)
    order = ",".join(cols)
    for row in conn.execute(
            f"SELECT {','.join(cols)} FROM {entity} ORDER BY {order}"):  # noqa: S608
        writer.writerow([row[c] for c in cols])
    return buf.getvalue()
