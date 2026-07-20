"""Load a directory of CSV files into the database.

Usage:  python -m app.load_sample [directory] [--db PATH]

Files are imported in dependency order; missing files are skipped.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import csv_io, db

IMPORT_ORDER = [
    "students", "teachers", "subjects", "rooms", "timeslots",
    "teacher_subjects", "student_needs",
    "teacher_availability", "student_availability",
]


def load_directory(directory: Path, db_path: Path) -> dict[str, int]:
    db.init_db(db_path)
    conn = db.connect(db_path)
    counts: dict[str, int] = {}
    try:
        for entity in IMPORT_ORDER:
            f = directory / f"{entity}.csv"
            if not f.exists():
                continue
            counts[entity] = csv_io.import_csv(
                conn, entity, f.read_text(encoding="utf-8-sig"))
    finally:
        conn.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", default="sample_data",
                        type=Path)
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    args = parser.parse_args()
    counts = load_directory(args.directory, args.db)
    for entity, n in counts.items():
        print(f"{entity}: {n} rows")
    if not counts:
        print(f"No CSV files found in {args.directory}")


if __name__ == "__main__":
    main()
