"""Tests for the three calendar view builders and their API endpoints."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.scheduler import Dataset, Lesson, Room, Timeslot
from app.views import build_overview, build_student_view, build_teacher_view

# 2026-07-27 is a Monday; 2026-07-29 a Wednesday.
MON, WED = "2026-07-27", "2026-07-29"


def make_data() -> Dataset:
    d = Dataset()
    d.students = {"s1": "Aoi", "s2": "Ren"}
    d.teachers = {"t1": "Tanaka", "t2": "Suzuki"}
    d.subjects = {"math": "Math", "eng": "English"}
    d.rooms = {"r1": Room("r1", "Room 1", 2)}
    d.timeslots = {
        "mon-1": Timeslot("mon-1", MON, 1, "09:00"),
        "mon-2": Timeslot("mon-2", MON, 2, "10:20"),
        "wed-1": Timeslot("wed-1", WED, 1, "09:00"),
    }
    return d


LESSONS = [
    Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
    Lesson("s2", "eng", "t2", "r1", "mon-1", id=2),
    Lesson("s2", "math", "t1", "r1", "mon-2", id=3),
    Lesson("s1", "eng", "t2", "r1", "wed-1", id=4),
]


def day_cell(view, date):
    for week in view["weeks"]:
        for cell in week:
            if cell["date"] == date:
                return cell
    raise AssertionError(f"no cell for {date}")


def slot_entries(view, date, period):
    cell = day_cell(view, date)
    for s in cell["slots"]:
        if s["period"] == period:
            return s["entries"]
    raise AssertionError(f"no slot {date} P{period}")


# ------------------------------------------------------------ calendar shape

def test_weeks_cover_whole_calendar_weeks():
    v = build_overview(make_data(), [])
    assert len(v["weeks"]) == 1                     # term fits in one week
    week = v["weeks"][0]
    assert len(week) == 7                           # always full Mon–Sun rows
    assert [c["weekday"] for c in week] == \
        ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert week[0]["date"] == MON                   # starts on the Monday
    assert v["periods"] == [1, 2]


def test_in_term_flags_and_slots():
    v = build_overview(make_data(), [])
    assert day_cell(v, MON)["in_term"] is True
    assert [s["period"] for s in day_cell(v, MON)["slots"]] == [1, 2]
    tue = day_cell(v, "2026-07-28")
    assert tue["in_term"] is False and tue["slots"] == []


def test_multi_week_term_produces_multiple_rows():
    d = make_data()
    d.timeslots["aug-1"] = Timeslot("aug-1", "2026-08-04", 1)  # next week Tue
    v = build_overview(d, [])
    assert len(v["weeks"]) == 2
    assert day_cell(v, "2026-08-04")["in_term"] is True


def test_empty_timeslots_no_weeks():
    d = make_data()
    d.timeslots = {}
    v = build_overview(d, [])
    assert v["weeks"] == [] and v["periods"] == []


def test_slot_label_carried_through():
    v = build_overview(make_data(), [])
    assert day_cell(v, MON)["slots"][0]["label"] == "09:00"


# ------------------------------------------------------------------ overview

def test_overview_groups_by_teacher_with_students():
    v = build_overview(make_data(), LESSONS)
    entries = slot_entries(v, MON, 1)
    assert [e["teacher_name"] for e in entries] == ["Suzuki", "Tanaka"]
    suzuki, tanaka = entries
    assert [l["student_name"] for l in tanaka["lessons"]] == ["Aoi"]
    assert tanaka["lessons"][0]["subject_name"] == "Math"
    assert tanaka["lessons"][0]["room_name"] == "Room 1"
    assert tanaka["lessons"][0]["lesson_id"] == 1
    assert [l["student_name"] for l in suzuki["lessons"]] == ["Ren"]


def test_overview_teacher_with_two_students_in_one_slot():
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s2", "math", "t1", "r1", "mon-1", id=2)]
    v = build_overview(make_data(), lessons)
    entries = slot_entries(v, MON, 1)
    assert len(entries) == 1
    assert [l["student_name"] for l in entries[0]["lessons"]] == ["Aoi", "Ren"]


def test_overview_ignores_lessons_with_unknown_slot():
    v = build_overview(make_data(),
                       [Lesson("s1", "math", "t1", "r1", "ghost", id=1)])
    for week in v["weeks"]:
        for cell in week:
            for slot in cell["slots"]:
                assert slot["entries"] == []


# -------------------------------------------------------------- student view

def test_student_view_shows_only_that_student():
    v = build_student_view(make_data(), LESSONS, "s1")
    assert v["student_name"] == "Aoi"
    mon1 = slot_entries(v, MON, 1)
    assert len(mon1) == 1
    assert mon1[0]["subject_name"] == "Math"
    assert mon1[0]["teacher_name"] == "Tanaka"
    assert "student_id" not in mon1[0]
    assert slot_entries(v, MON, 2) == []            # that's s2's lesson
    assert slot_entries(v, WED, 1)[0]["subject_name"] == "English"


def test_student_view_unknown_student_raises():
    with pytest.raises(KeyError):
        build_student_view(make_data(), LESSONS, "ghost")


# -------------------------------------------------------------- teacher view

def test_teacher_view_shows_subject_and_student():
    v = build_teacher_view(make_data(), LESSONS, "t1")
    assert v["teacher_name"] == "Tanaka"
    mon1 = slot_entries(v, MON, 1)
    assert len(mon1) == 1
    assert mon1[0]["subject_name"] == "Math"
    assert mon1[0]["student_name"] == "Aoi"
    assert "teacher_id" not in mon1[0]
    assert slot_entries(v, MON, 2)[0]["student_name"] == "Ren"
    assert slot_entries(v, WED, 1) == []            # that's t2's lesson


def test_teacher_view_multiple_students_sorted_by_name():
    lessons = [Lesson("s2", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s1", "eng", "t1", "r1", "mon-1", id=2)]
    v = build_teacher_view(make_data(), lessons, "t1")
    names = [e["student_name"] for e in slot_entries(v, MON, 1)]
    assert names == ["Aoi", "Ren"]


def test_teacher_view_unknown_teacher_raises():
    with pytest.raises(KeyError):
        build_teacher_view(make_data(), LESSONS, "ghost")


# ----------------------------------------------------------------- endpoints

@pytest.fixture
def client(tmp_path):
    app.state.db_path = tmp_path / "views.db"
    with TestClient(app) as c:
        yield c
    del app.state.db_path


def seed(client):
    for path, body in [
        ("/api/students", {"id": "s1", "name": "Aoi"}),
        ("/api/teachers", {"id": "t1", "name": "Tanaka"}),
        ("/api/subjects", {"id": "math", "name": "Math"}),
        ("/api/rooms", {"id": "r1", "name": "Room 1", "capacity": 1}),
        ("/api/timeslots", {"id": "mon-1", "date": MON, "period": 1,
                            "label": "09:00"}),
        ("/api/teacher_subjects", {"teacher_id": "t1", "subject_id": "math"}),
        ("/api/teacher_availability", {"teacher_id": "t1",
                                       "timeslot_id": "mon-1"}),
        ("/api/student_availability", {"student_id": "s1",
                                       "timeslot_id": "mon-1"}),
        ("/api/lessons", {"student_id": "s1", "subject_id": "math",
                          "teacher_id": "t1", "room_id": "r1",
                          "timeslot_id": "mon-1"}),
    ]:
        assert client.post(path, json=body).status_code == 200, path


def test_endpoint_overview(client):
    seed(client)
    v = client.get("/api/views/overview").json()
    entries = slot_entries(v, MON, 1)
    assert entries[0]["teacher_name"] == "Tanaka"
    assert entries[0]["lessons"][0]["student_name"] == "Aoi"


def test_endpoint_student_view(client):
    seed(client)
    v = client.get("/api/views/student/s1").json()
    assert v["student_name"] == "Aoi"
    assert slot_entries(v, MON, 1)[0]["subject_name"] == "Math"
    assert client.get("/api/views/student/ghost").status_code == 404


def test_endpoint_teacher_view(client):
    seed(client)
    v = client.get("/api/views/teacher/t1").json()
    assert v["teacher_name"] == "Tanaka"
    assert slot_entries(v, MON, 1)[0]["student_name"] == "Aoi"
    assert client.get("/api/views/teacher/ghost").status_code == 404
