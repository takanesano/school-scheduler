"""End-to-end tests of the REST API against a temporary database."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path):
    app.state.db_path = tmp_path / "api.db"
    with TestClient(app) as c:
        yield c
    del app.state.db_path


def seed_world(client):
    """Minimal consistent world used by several tests."""
    for path, body in [
        ("/api/students", {"id": "s1", "name": "Aoi"}),
        ("/api/students", {"id": "s2", "name": "Ren"}),
        ("/api/teachers", {"id": "t1", "name": "Tanaka"}),
        ("/api/subjects", {"id": "math", "name": "Math"}),
        ("/api/rooms", {"id": "r1", "name": "Room 1", "capacity": 1}),
        ("/api/timeslots", {"id": "mon-1", "date": "2026-07-27", "period": 1}),
        ("/api/timeslots", {"id": "mon-2", "date": "2026-07-27", "period": 2}),
        ("/api/timeslots", {"id": "tue-1", "date": "2026-07-28", "period": 1}),
        ("/api/teacher_subjects", {"teacher_id": "t1", "subject_id": "math"}),
    ]:
        assert client.post(path, json=body).status_code == 200, path
    for slot in ("mon-1", "mon-2", "tue-1"):
        client.post("/api/teacher_availability",
                    json={"teacher_id": "t1", "timeslot_id": slot})
        for st in ("s1", "s2"):
            client.post("/api/student_availability",
                        json={"student_id": st, "timeslot_id": slot})


# ---------------------------------------------------------------------- CRUD

def test_crud_students(client):
    assert client.get("/api/students").json() == []
    assert client.post("/api/students",
                       json={"id": "s1", "name": "Aoi"}).status_code == 200
    assert client.get("/api/students").json() == [{"id": "s1", "name": "Aoi"}]
    # upsert updates the name
    client.post("/api/students", json={"id": "s1", "name": "Aoi K."})
    assert client.get("/api/students").json() == [{"id": "s1", "name": "Aoi K."}]
    assert client.delete("/api/students/s1").status_code == 200
    assert client.get("/api/students").json() == []
    assert client.delete("/api/students/s1").status_code == 404


def test_validation_rejects_blank_id(client):
    assert client.post("/api/students",
                       json={"id": "", "name": "X"}).status_code == 422


def test_room_capacity_must_be_positive(client):
    r = client.post("/api/rooms", json={"id": "r1", "name": "R", "capacity": 0})
    assert r.status_code == 422


def test_timeslot_bad_date_rejected(client):
    r = client.post("/api/timeslots",
                    json={"id": "x", "date": "Monday", "period": 1})
    assert r.status_code == 422
    r = client.post("/api/timeslots",
                    json={"id": "x", "date": "2026-02-30", "period": 1})
    assert r.status_code == 422


def test_timeslot_duplicate_day_period_conflict(client):
    assert client.post("/api/timeslots",
                       json={"id": "a", "date": "2026-07-27", "period": 1}).status_code == 200
    r = client.post("/api/timeslots",
                    json={"id": "b", "date": "2026-07-27", "period": 1})
    assert r.status_code == 409


def test_link_rejects_unknown_reference(client):
    r = client.post("/api/teacher_subjects",
                    json={"teacher_id": "ghost", "subject_id": "math"})
    assert r.status_code == 422


def test_unknown_entity_404(client):
    assert client.get("/api/wizards").status_code == 404


def test_deleting_student_cascades(client):
    seed_world(client)
    client.post("/api/student_needs",
                json={"student_id": "s1", "subject_id": "math",
                      "sessions": 1})
    client.delete("/api/students/s1")
    assert client.get("/api/student_needs").json() == []
    assert all(r["student_id"] != "s1"
               for r in client.get("/api/student_availability").json())


# ----------------------------------------------------------------------- CSV

def test_csv_import_export_cycle(client):
    csv_text = "id,name\ns1,Aoi\ns2,Ren\n"
    r = client.post("/api/import/students",
                    files={"file": ("students.csv", csv_text, "text/csv")})
    assert r.status_code == 200 and r.json()["rows"] == 2
    out = client.get("/api/export/students")
    assert out.status_code == 200
    assert out.text == csv_text


def test_csv_import_invalid_returns_errors(client):
    r = client.post("/api/import/students",
                    files={"file": ("students.csv", "id,name\ns1,\n", "text/csv")})
    assert r.status_code == 422
    assert "Line 2" in r.json()["detail"]["errors"][0]


def test_csv_import_unknown_entity(client):
    r = client.post("/api/import/wizards",
                    files={"file": ("w.csv", "id,name\n", "text/csv")})
    assert r.status_code == 404


def test_csv_import_non_utf8(client):
    r = client.post("/api/import/students",
                    files={"file": ("s.csv", "id,name\ns1,Aoi".encode("utf-16"),
                                    "text/csv")})
    assert r.status_code == 422


# ------------------------------------------------------------ bulk timeslots

def test_bulk_timeslots_creates_range(client):
    r = client.post("/api/timeslots/bulk", json={
        "start_date": "2026-07-27", "end_date": "2026-08-02",   # Mon..Sun
        "weekdays": ["Mon", "Wed"],
        "periods": [{"period": 1, "label": "09:00-10:10"},
                    {"period": 2, "label": ""}]})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "created": 4, "skipped": 0}
    slots = client.get("/api/timeslots").json()
    assert {(s["id"], s["date"], s["period"], s["label"]) for s in slots} == {
        ("0727-1", "2026-07-27", 1, "09:00-10:10"),
        ("0727-2", "2026-07-27", 2, ""),
        ("0729-1", "2026-07-29", 1, "09:00-10:10"),
        ("0729-2", "2026-07-29", 2, ""),
    }


def test_bulk_timeslots_skips_existing_pairs(client):
    client.post("/api/timeslots", json={"id": "mine", "date": "2026-07-27",
                                        "period": 1, "label": "custom"})
    r = client.post("/api/timeslots/bulk", json={
        "start_date": "2026-07-27", "end_date": "2026-07-27",
        "weekdays": ["Mon"], "periods": [{"period": 1}, {"period": 2}]})
    assert r.json() == {"ok": True, "created": 1, "skipped": 1}
    slots = {s["id"]: s for s in client.get("/api/timeslots").json()}
    assert slots["mine"]["label"] == "custom"      # untouched
    assert "0727-2" in slots


def test_bulk_timeslots_id_collision_falls_back_to_long_id(client):
    # id "0727-1" already used by a DIFFERENT date
    client.post("/api/timeslots", json={"id": "0727-1", "date": "2026-01-05",
                                        "period": 9})
    r = client.post("/api/timeslots/bulk", json={
        "start_date": "2026-07-27", "end_date": "2026-07-27",
        "weekdays": ["Mon"], "periods": [{"period": 1}]})
    assert r.json()["created"] == 1
    slots = {s["id"]: s for s in client.get("/api/timeslots").json()}
    assert slots["20260727-1"]["date"] == "2026-07-27"


@pytest.mark.parametrize("body,fragment", [
    ({"start_date": "bad", "end_date": "2026-08-01",
      "weekdays": ["Mon"], "periods": [{"period": 1}]}, "YYYY-MM-DD"),
    ({"start_date": "2026-08-02", "end_date": "2026-08-01",
      "weekdays": ["Mon"], "periods": [{"period": 1}]}, "after"),
    ({"start_date": "2026-01-01", "end_date": "2027-06-01",
      "weekdays": ["Mon"], "periods": [{"period": 1}]}, "400 days"),
    ({"start_date": "2026-07-27", "end_date": "2026-08-01",
      "weekdays": ["Monday"], "periods": [{"period": 1}]}, "weekday"),
    ({"start_date": "2026-07-27", "end_date": "2026-08-01",
      "weekdays": [], "periods": [{"period": 1}]}, "at least one weekday"),
    ({"start_date": "2026-07-27", "end_date": "2026-08-01",
      "weekdays": ["Mon"], "periods": []}, "at least one period"),
    ({"start_date": "2026-07-27", "end_date": "2026-08-01",
      "weekdays": ["Mon"],
      "periods": [{"period": 1}, {"period": 1}]}, "Duplicate"),
])
def test_bulk_timeslots_validation(client, body, fragment):
    r = client.post("/api/timeslots/bulk", json=body)
    assert r.status_code == 422
    assert fragment in r.json()["detail"]
    assert client.get("/api/timeslots").json() == []   # nothing created


# ------------------------------------------------------------------ schedule

def test_manual_add_rejects_student_day_gap(client):
    seed_world(client)
    client.post("/api/timeslots", json={"id": "mon-3", "date": "2026-07-27",
                                        "period": 3})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t1", "timeslot_id": "mon-3"})
    client.post("/api/student_availability",
                json={"student_id": "s1", "timeslot_id": "mon-3"})
    base = {"student_id": "s1", "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1"}
    assert client.post("/api/lessons",
                       json=dict(base, timeslot_id="mon-1")).status_code == 200
    # P1 then P3 on the same day: not consecutive -> rejected
    r = client.post("/api/lessons", json=dict(base, timeslot_id="mon-3"))
    assert r.status_code == 409
    codes = {v["code"] for v in r.json()["detail"]["violations"]}
    assert "student_day_gap" in codes
    # P1 then P2 is fine
    assert client.post("/api/lessons",
                       json=dict(base, timeslot_id="mon-2")).status_code == 200
    assert client.get("/api/schedule").json()["violations"] == []


def test_generate_and_fetch_schedule(client):
    seed_world(client)
    for st, n in (("s1", 2), ("s2", 1)):
        client.post("/api/student_needs",
                    json={"student_id": st, "subject_id": "math",
                          "sessions": n})
    r = client.post("/api/schedule/generate",
                    json={"keep_existing": False})
    assert r.status_code == 200
    body = r.json()
    assert body["complete"] is True
    assert body["scheduled"] == 3
    assert body["unscheduled"] == []

    sched = client.get("/api/schedule").json()
    assert len(sched["lessons"]) == 3
    assert sched["violations"] == []
    assert sched["coverage"] == []


def test_generate_partial_reports_unscheduled(client):
    seed_world(client)
    # 2 students × 2 math sessions with spread-on and 2 days: fine.
    # But teacher t1 has only 3 available slots total → 4 needed, 3 possible.
    for st in ("s1", "s2"):
        client.post("/api/student_needs",
                    json={"student_id": st, "subject_id": "math",
                          "sessions": 2})
    r = client.post("/api/schedule/generate",
                    json={"keep_existing": False})
    body = r.json()
    assert body["complete"] is False
    assert body["scheduled"] == 3
    assert sum(u["missing"] for u in body["unscheduled"]) == 1
    # partial schedule must still be conflict-free (validated with the same
    # spread setting it was generated with)
    sched = client.get(
        "/api/schedule").json()
    assert sched["violations"] == []


def test_schedule_reports_teacher_stats(client):
    seed_world(client)
    client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    client.post("/api/lessons", json={
        "student_id": "s2", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "tue-1"})
    body = client.get("/api/schedule").json()
    assert body["teacher_stats"] == [
        {"teacher_id": "t1", "name": "Tanaka", "lessons": 2, "days": 2}]
    assert body["objective"] == {"student_double_days": 0, "slot_spread": 0,
                                 "total_days": 2, "day_spread": 0}
    assert body["student_stats"] == [
        {"student_id": "s1", "name": "Aoi", "lessons": 1, "days": 1,
         "double_days": []},
        {"student_id": "s2", "name": "Ren", "lessons": 1, "days": 1,
         "double_days": []}]


def test_schedule_reports_student_double_days(client):
    seed_world(client)
    for slot in ("mon-1", "mon-2"):
        client.post("/api/lessons", json={
            "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1", "timeslot_id": slot})
    body = client.get("/api/schedule").json()
    assert body["violations"] == []          # consecutive pair is legal
    s1 = next(s for s in body["student_stats"] if s["student_id"] == "s1")
    assert s1 == {"student_id": "s1", "name": "Aoi", "lessons": 2,
                  "days": 1, "double_days": ["2026-07-27"]}
    assert body["objective"]["student_double_days"] == 1


def test_generate_compresses_teacher_days(client):
    """Both students can meet on Monday; without compression the solver's
    chronological greed still finds Monday here, so give it a layout where
    compression provably matters: s1 Mon-only, s2 Mon+Tue, and check the
    optimizer keeps everything on Monday."""
    seed_world(client)
    client.post("/api/rooms", json={"id": "r2", "name": "Room 2"})
    for st, n in (("s1", 1), ("s2", 1)):
        client.post("/api/student_needs",
                    json={"student_id": st, "subject_id": "math",
                          "sessions": n})
    r = client.post("/api/schedule/generate",
                    json={"keep_existing": False,
                          "compress_teacher_days": True})
    assert r.json()["complete"] is True
    body = client.get("/api/schedule").json()
    assert body["violations"] == []
    # t1 teaches both lessons and they share a single working day
    assert body["teacher_stats"][0]["lessons"] == 2
    assert body["teacher_stats"][0]["days"] == 1
    assert body["objective"]["total_days"] == 1


def test_generate_with_v2_solver(client):
    pytest.importorskip("ortools")
    seed_world(client)
    for st, n in (("s1", 2), ("s2", 1)):
        client.post("/api/student_needs",
                    json={"student_id": st, "subject_id": "math",
                          "sessions": n})
    r = client.post("/api/schedule/generate", json={"solver": "v2"})
    assert r.status_code == 200
    body = r.json()
    assert body["complete"] is True
    assert body["backend"] == "cpsat"
    sched = client.get("/api/schedule").json()
    assert sched["violations"] == [] and sched["coverage"] == []


def test_generate_rejects_unknown_solver(client):
    r = client.post("/api/schedule/generate", json={"solver": "v3"})
    assert r.status_code == 422


@pytest.mark.parametrize("budget", [0, 0.5, 601, -3])
def test_generate_rejects_out_of_range_v2_budget(client, budget):
    r = client.post("/api/schedule/generate",
                    json={"solver": "v2", "v2_time_budget": budget})
    assert r.status_code == 422


@pytest.mark.parametrize("order", [
    ["student_double_day"],                              # incomplete
    ["a", "b", "c", "d"],                                # unknown names
    ["student_double_day", "student_double_day",
     "teacher_working_day", "teacher_day_spread"],       # duplicate
])
def test_generate_rejects_bad_objective_order(client, order):
    r = client.post("/api/schedule/generate",
                    json={"objective_order": order})
    assert r.status_code == 422
    assert "permutation" in r.json()["detail"]


def test_generate_honors_objective_order(client):
    """Days-first priority keeps both lessons on one teacher-day where
    the default balance-first order would use two teachers."""
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "math"})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t2", "timeslot_id": "tue-1"})
    client.post("/api/rooms", json={"id": "r2", "name": "Room 2"})
    for st in ("s1", "s2"):
        client.post("/api/student_needs",
                    json={"student_id": st, "subject_id": "math",
                          "sessions": 1})
    days_first = ["student_double_day", "teacher_working_day",
                  "teacher_slot_spread", "teacher_day_spread"]
    r = client.post("/api/schedule/generate",
                    json={"objective_order": days_first})
    assert r.json()["complete"] is True
    body = client.get("/api/schedule").json()
    assert body["objective"]["total_days"] == 1
    teachers = {l["teacher_id"] for l in body["lessons"]}
    assert len(teachers) == 1


# ------------------------------------------------------------------ settings

def test_settings_defaults_and_roundtrip(client):
    assert client.get("/api/settings").json() == {
        "teacher_capacity": 2, "student_day_cap": 2,
        "require_consecutive": True, "objective_caps": {}}
    r = client.put("/api/settings", json={
        "teacher_capacity": 1, "student_day_cap": 3,
        "require_consecutive": False,
        "objective_caps": {"teacher_slot_spread": 1}})
    assert r.status_code == 200
    assert client.get("/api/settings").json() == {
        "teacher_capacity": 1, "student_day_cap": 3,
        "require_consecutive": False,
        "objective_caps": {"teacher_slot_spread": 1}}


@pytest.mark.parametrize("body", [
    {"teacher_capacity": 0},
    {"student_day_cap": 9},
    {"objective_caps": {"nonsense": 1}},
    {"objective_caps": {"teacher_slot_spread": -1}},
])
def test_settings_validation(client, body):
    assert client.put("/api/settings", json=body).status_code == 422


def test_settings_drive_validation_and_manual_adds(client):
    seed_world(client)
    client.put("/api/settings", json={"student_day_cap": 1})
    base = {"student_id": "s1", "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1"}
    assert client.post("/api/lessons",
                       json=dict(base, timeslot_id="mon-1")).status_code == 200
    r = client.post("/api/lessons", json=dict(base, timeslot_id="mon-2"))
    assert r.status_code == 409
    codes = {v["code"] for v in r.json()["detail"]["violations"]}
    assert "student_day_overload" in codes
    # relax back to 2 -> same add is now clean
    client.put("/api/settings", json={"student_day_cap": 2})
    assert client.post("/api/lessons",
                       json=dict(base, timeslot_id="mon-2")).status_code == 200
    assert client.get("/api/schedule").json()["violations"] == []


def test_settings_consecutive_off_allows_gap(client):
    seed_world(client)
    client.post("/api/timeslots", json={"id": "mon-3", "date": "2026-07-27",
                                        "period": 3})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t1", "timeslot_id": "mon-3"})
    client.post("/api/student_availability",
                json={"student_id": "s1", "timeslot_id": "mon-3"})
    client.put("/api/settings", json={"require_consecutive": False})
    base = {"student_id": "s1", "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1"}
    assert client.post("/api/lessons",
                       json=dict(base, timeslot_id="mon-1")).status_code == 200
    assert client.post("/api/lessons",
                       json=dict(base, timeslot_id="mon-3")).status_code == 200
    assert client.get("/api/schedule").json()["violations"] == []


def test_promoted_objective_cap_reported_in_status(client):
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "math"})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t2", "timeslot_id": "tue-1"})
    # both lessons on t1 -> slot spread 2
    for slot in ("mon-1", "tue-1"):
        client.post("/api/lessons", json={
            "student_id": "s1" if slot == "mon-1" else "s2",
            "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1", "timeslot_id": slot})
    assert client.get("/api/schedule").json()["violations"] == []
    client.put("/api/settings",
               json={"objective_caps": {"teacher_slot_spread": 1}})
    codes = {v["code"] for v in
             client.get("/api/schedule").json()["violations"]}
    assert codes == {"objective_cap_exceeded"}


def test_generate_v2_accepts_small_budget(client):
    pytest.importorskip("ortools")
    seed_world(client)
    client.post("/api/student_needs",
                json={"student_id": "s1", "subject_id": "math",
                      "sessions": 1})
    r = client.post("/api/schedule/generate",
                    json={"solver": "v2", "v2_time_budget": 1})
    assert r.status_code == 200
    assert r.json()["complete"] is True
    assert client.get("/api/schedule").json()["violations"] == []


def test_input_problem_diagnostics(client):
    seed_world(client)
    client.post("/api/subjects", json={"id": "eng", "name": "English"})
    client.post("/api/student_needs",
                json={"student_id": "s1", "subject_id": "eng",
                      "sessions": 1})
    probs = client.get("/api/schedule/check").json()["problems"]
    assert len(probs) == 1
    assert "No teacher can teach English" in probs[0]


def test_manual_lesson_valid(client):
    seed_world(client)
    r = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    assert r.status_code == 200
    assert client.get("/api/schedule").json()["violations"] == []


def test_manual_lesson_pairing_ok_but_room_conflict_rejected(client):
    seed_world(client)
    base = {"student_id": "s1", "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1", "timeslot_id": "mon-1"}
    assert client.post("/api/lessons", json=base).status_code == 200
    # same teacher, same slot is fine now (pairing), but room r1 has
    # capacity 1 -> rejected because of the room
    conflict = dict(base, student_id="s2")
    r = client.post("/api/lessons", json=conflict)
    assert r.status_code == 409
    msgs = [v["message"] for v in r.json()["detail"]["violations"]]
    assert any("Room 1" in m for m in msgs)
    assert not any("Tanaka" in m for m in msgs)   # teacher is NOT the issue
    # force through
    r = client.post("/api/lessons", json=dict(conflict, force=True))
    assert r.status_code == 200
    codes = {v["code"] for v in client.get("/api/schedule").json()["violations"]}
    assert codes == {"room_over_capacity"}


def test_manual_lesson_pairing_with_room_space_is_clean(client):
    seed_world(client)
    client.post("/api/rooms", json={"id": "r2", "name": "Room 2", "capacity": 2})
    base = {"student_id": "s1", "subject_id": "math", "teacher_id": "t1",
            "room_id": "r2", "timeslot_id": "mon-1"}
    assert client.post("/api/lessons", json=base).status_code == 200
    r = client.post("/api/lessons", json=dict(base, student_id="s2"))
    assert r.status_code == 200
    assert client.get("/api/schedule").json()["violations"] == []


def test_move_lesson_to_free_slot(client):
    seed_world(client)
    r = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    lid = r.json()["id"]
    r = client.patch(f"/api/lessons/{lid}", json={"timeslot_id": "tue-1"})
    assert r.status_code == 200
    assert r.json()["lesson"]["timeslot_id"] == "tue-1"
    lessons = client.get("/api/schedule").json()["lessons"]
    assert [l["timeslot_id"] for l in lessons] == ["tue-1"]
    assert client.get("/api/schedule").json()["violations"] == []


def test_move_lesson_conflict_rejected_then_forced(client):
    seed_world(client)
    client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    r2 = client.post("/api/lessons", json={
        "student_id": "s2", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "tue-1"})
    lid = r2.json()["id"]
    # moving s2's lesson onto mon-1 overfills room r1 (capacity 1);
    # sharing teacher t1 alone would be fine (pairing)
    r = client.patch(f"/api/lessons/{lid}", json={"timeslot_id": "mon-1"})
    assert r.status_code == 409
    codes = {v["code"] for v in r.json()["detail"]["violations"]}
    assert "room_over_capacity" in codes
    # nothing changed
    lessons = client.get("/api/schedule").json()["lessons"]
    assert sorted(l["timeslot_id"] for l in lessons) == ["mon-1", "tue-1"]
    # force it through
    r = client.patch(f"/api/lessons/{lid}",
                     json={"timeslot_id": "mon-1", "force": True})
    assert r.status_code == 200
    codes = {v["code"] for v in client.get("/api/schedule").json()["violations"]}
    assert "room_over_capacity" in codes


def test_move_lesson_can_change_teacher_and_room(client):
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "math"})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t2", "timeslot_id": "mon-1"})
    client.post("/api/rooms", json={"id": "r2", "name": "Room 2"})
    r = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    lid = r.json()["id"]
    r = client.patch(f"/api/lessons/{lid}",
                     json={"teacher_id": "t2", "room_id": "r2"})
    assert r.status_code == 200
    assert client.get("/api/schedule").json()["violations"] == []


def test_move_lesson_unknown_lesson_404(client):
    seed_world(client)
    assert client.patch("/api/lessons/999",
                        json={"timeslot_id": "mon-1"}).status_code == 404


def test_move_lesson_unknown_slot_422(client):
    seed_world(client)
    r = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    lid = r.json()["id"]
    r = client.patch(f"/api/lessons/{lid}", json={"timeslot_id": "ghost"})
    assert r.status_code == 422
    # force cannot bypass an unknown reference either
    r = client.patch(f"/api/lessons/{lid}",
                     json={"timeslot_id": "ghost", "force": True})
    assert r.status_code == 422


def test_check_options_reports_per_option_problems(client):
    seed_world(client)
    client.post("/api/subjects", json={"id": "eng", "name": "English"})
    r = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    lid = r.json()["id"]
    res = client.post(f"/api/lessons/{lid}/check_options", json={})
    assert res.status_code == 200
    body = res.json()
    # current combination is valid
    assert body["current"] == []
    # keeping math with t1 is fine; switching to eng is impossible for t1
    assert body["subjects"]["math"] == []
    assert any("cannot teach" in m for m in body["subjects"]["eng"])
    assert body["teachers"]["t1"] == []
    assert body["rooms"]["r1"] == []


def test_check_options_room_capacity_and_teacher_clash(client):
    seed_world(client)
    client.post("/api/students", json={"id": "s3", "name": "Yui"})
    client.post("/api/student_availability",
                json={"student_id": "s3", "timeslot_id": "mon-1"})
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "math"})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t2", "timeslot_id": "mon-1"})
    client.post("/api/rooms", json={"id": "r2", "name": "Room 2",
                                    "capacity": 3})
    # t1 already teaches TWO students at mon-1 (pairing limit reached);
    # r1 (capacity 1) is full
    client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    client.post("/api/lessons", json={
        "student_id": "s3", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r2", "timeslot_id": "mon-1"})
    r = client.post("/api/lessons", json={
        "student_id": "s2", "subject_id": "math", "teacher_id": "t2",
        "room_id": "r2", "timeslot_id": "mon-1"})
    lid = r.json()["id"]
    body = client.post(f"/api/lessons/{lid}/check_options", json={}).json()
    assert body["current"] == []
    assert any("max 2 at once" in m for m in body["teachers"]["t1"])
    assert any("capacity" in m for m in body["rooms"]["r1"])        # r1 full
    assert body["teachers"]["t2"] == []
    assert body["rooms"]["r2"] == []


def test_check_options_holds_proposed_fields_fixed(client):
    seed_world(client)
    client.post("/api/subjects", json={"id": "eng", "name": "English"})
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "eng"})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t2", "timeslot_id": "mon-1"})
    r = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    lid = r.json()["id"]
    # propose switching the subject to eng: now t1 is the bad teacher option
    body = client.post(f"/api/lessons/{lid}/check_options",
                       json={"subject_id": "eng"}).json()
    assert any("cannot teach" in m for m in body["current"])   # t1 + eng
    assert any("cannot teach" in m for m in body["teachers"]["t1"])
    assert body["teachers"]["t2"] == []


def test_check_options_unknown_lesson_404(client):
    seed_world(client)
    assert client.post("/api/lessons/999/check_options",
                       json={}).status_code == 404


def test_delete_lesson_and_clear(client):
    seed_world(client)
    r = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    lid = r.json()["id"]
    assert client.delete(f"/api/lessons/{lid}").status_code == 200
    assert client.delete(f"/api/lessons/{lid}").status_code == 404
    client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    assert client.delete("/api/schedule").status_code == 200
    assert client.get("/api/schedule").json()["lessons"] == []


def test_generate_keep_existing(client):
    seed_world(client)
    client.post("/api/student_needs",
                json={"student_id": "s1", "subject_id": "math",
                      "sessions": 2})
    r = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "tue-1"})
    assert r.status_code == 200
    body = client.post("/api/schedule/generate",
                       json={"keep_existing": True}).json()
    assert body["complete"] is True
    lessons = client.get("/api/schedule").json()["lessons"]
    assert len(lessons) == 2
    assert any(l["timeslot_id"] == "tue-1" for l in lessons)


def test_index_serves_ui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Cram School Scheduler" in r.text
