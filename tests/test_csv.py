"""Tests for CSV parsing, atomic import, and export round-trips."""
import pytest

from app import csv_io, db


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


def rows(conn, table):
    return [tuple(r) for r in conn.execute(
        f"SELECT * FROM {table} ORDER BY 1, 2")]


# ------------------------------------------------------------------- parsing

def test_parse_valid_students():
    got = csv_io.parse_csv("students", "id,name\ns1,Aoi\ns2,Ren\n")
    assert got == [{"id": "s1", "name": "Aoi"}, {"id": "s2", "name": "Ren"}]


def test_parse_strips_whitespace_and_bom():
    got = csv_io.parse_csv("students", "﻿id, name\n s1 , Aoi \n")
    assert got == [{"id": "s1", "name": "Aoi"}]


def test_parse_missing_column():
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv("students", "id\ns1\n")
    assert "Missing column(s): name" in e.value.errors[0]


def test_parse_empty_file():
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv("students", "")
    assert "empty" in e.value.errors[0].lower()


def test_parse_empty_value_has_line_number():
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv("students", "id,name\ns1,Aoi\ns2,\n")
    assert e.value.errors == ["Line 3: 'name' is empty"]


def test_parse_duplicate_id():
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv("students", "id,name\ns1,Aoi\ns1,Ren\n")
    assert "Line 3: duplicate" in e.value.errors[0]


def test_parse_duplicate_composite_key():
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv("teacher_subjects",
                         "teacher_id,subject_id\nt1,math\nt1,math\n")
    assert "duplicate" in e.value.errors[0]


def test_parse_bad_capacity():
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv("rooms", "id,name,capacity\nr1,Room,zero\n")
    assert "capacity" in e.value.errors[0]


def test_parse_capacity_defaults_to_one():
    got = csv_io.parse_csv("rooms", "id,name\nr1,Room\n")
    assert got == [{"id": "r1", "name": "Room", "capacity": "1",
                    "teacher_capacity": "0"}]


def test_parse_teacher_day_max_defaults_to_zero():
    got = csv_io.parse_csv("teachers", "id,name\nt1,Tanaka\n")
    assert got == [{"id": "t1", "name": "Tanaka",
                    "max_lessons_per_day": "0"}]


def test_parse_bad_teacher_day_max():
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv(
            "teachers", "id,name,max_lessons_per_day\nt1,Tanaka,-2\n")
    assert "max_lessons_per_day" in e.value.errors[0]


def test_parse_bad_teacher_capacity():
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv(
            "rooms", "id,name,capacity,teacher_capacity\nr1,Room,2,-1\n")
    assert "teacher_capacity" in e.value.errors[0]


@pytest.mark.parametrize("bad", ["Monday", "2026/07/27", "2026-13-01",
                                 "2026-02-30", "20260727"])
def test_parse_bad_date(bad):
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv("timeslots", f"id,date,period\nx,{bad},1\n")
    assert "date must be YYYY-MM-DD" in e.value.errors[0]


def test_parse_bad_sessions():
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv("student_needs",
                         "student_id,subject_id,sessions\ns1,math,0\n")
    assert "sessions" in e.value.errors[0]


def test_parse_collects_all_errors():
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.parse_csv("students", "id,name\n,Aoi\ns2,\n")
    assert len(e.value.errors) == 2


def test_parse_unknown_entity():
    with pytest.raises(csv_io.CsvError):
        csv_io.parse_csv("wizards", "id,name\nw1,Merlin\n")


# -------------------------------------------------------------------- import

def test_import_students(conn):
    n = csv_io.import_csv(conn, "students", "id,name\ns1,Aoi\ns2,Ren\n")
    assert n == 2
    assert rows(conn, "students") == [("s1", "Aoi"), ("s2", "Ren")]


def test_import_replaces_contents(conn):
    csv_io.import_csv(conn, "students", "id,name\ns1,Aoi\ns2,Ren\n")
    csv_io.import_csv(conn, "students", "id,name\ns3,Yui\n")
    assert rows(conn, "students") == [("s3", "Yui")]


def test_reimport_same_parent_keeps_children(conn):
    """Re-importing students.csv must not cascade-delete availability."""
    csv_io.import_csv(conn, "students", "id,name\ns1,Aoi\n")
    csv_io.import_csv(conn, "timeslots", "id,date,period,label\nmon-1,2026-07-27,1,\n")
    csv_io.import_csv(conn, "student_availability",
                      "student_id,timeslot_id\ns1,mon-1\n")
    csv_io.import_csv(conn, "students", "id,name\ns1,Aoi Renamed\ns2,New\n")
    assert rows(conn, "student_availability") == [("s1", "mon-1")]
    assert ("s1", "Aoi Renamed") in rows(conn, "students")


def test_import_removed_parent_cascades(conn):
    csv_io.import_csv(conn, "students", "id,name\ns1,Aoi\n")
    csv_io.import_csv(conn, "timeslots", "id,date,period,label\nmon-1,2026-07-27,1,\n")
    csv_io.import_csv(conn, "student_availability",
                      "student_id,timeslot_id\ns1,mon-1\n")
    csv_io.import_csv(conn, "students", "id,name\ns2,Other\n")  # s1 gone
    assert rows(conn, "student_availability") == []


def test_import_is_atomic_on_error(conn):
    csv_io.import_csv(conn, "students", "id,name\ns1,Aoi\n")
    with pytest.raises(csv_io.CsvError):
        csv_io.import_csv(conn, "students", "id,name\ns2,Ren\ns3,\n")
    assert rows(conn, "students") == [("s1", "Aoi")]  # unchanged


def test_import_link_rejects_unknown_reference(conn):
    csv_io.import_csv(conn, "teachers", "id,name\nt1,Tanaka\n")
    with pytest.raises(csv_io.CsvError) as e:
        csv_io.import_csv(conn, "teacher_subjects",
                          "teacher_id,subject_id\nt1,math\n")
    assert any("does not exist in subjects" in msg for msg in e.value.errors)
    assert rows(conn, "teacher_subjects") == []


def test_import_link_ok_when_references_exist(conn):
    csv_io.import_csv(conn, "teachers", "id,name\nt1,Tanaka\n")
    csv_io.import_csv(conn, "subjects", "id,name\nmath,Math\n")
    n = csv_io.import_csv(conn, "teacher_subjects",
                          "teacher_id,subject_id\nt1,math\n")
    assert n == 1
    assert rows(conn, "teacher_subjects") == [("t1", "math")]


def test_import_needs_full_pipeline(conn):
    csv_io.import_csv(conn, "students", "id,name\ns1,Aoi\n")
    csv_io.import_csv(conn, "subjects", "id,name\nmath,Math\n")
    n = csv_io.import_csv(
        conn, "student_needs",
        "student_id,subject_id,sessions\ns1,math,2\n")
    assert n == 1
    assert rows(conn, "student_needs") == [("s1", "math", 2)]


# -------------------------------------------------------------------- export

@pytest.mark.parametrize("entity,csv_text", [
    ("students", "id,name\ns1,Aoi\ns2,Ren\n"),
    ("rooms", "id,name,capacity,teacher_capacity\nr1,Room 1,2,6\n"),
    ("teachers", "id,name,max_lessons_per_day\nt1,Tanaka,4\n"),
    ("timeslots", "id,date,period,label\nmon-1,2026-07-27,1,17:00\n"),
])
def test_export_round_trip(conn, entity, csv_text):
    csv_io.import_csv(conn, entity, csv_text)
    assert csv_io.export_csv(conn, entity) == csv_text


def test_export_handles_commas_and_quotes(conn):
    csv_io.import_csv(conn, "students", 'id,name\ns1,"Doe, ""JJ"" Jane"\n')
    out = csv_io.export_csv(conn, "students")
    assert csv_io.parse_csv("students", out) == [
        {"id": "s1", "name": 'Doe, "JJ" Jane'}]


def test_export_empty_table_has_header(conn):
    assert csv_io.export_csv(conn, "students") == "id,name\n"
