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


# -------------------------------------------------------- mass-edit timeslots

def seed_slot_grid(client):
    """Mon-Sat × periods 1-2 over two weeks, no penalties, no labels."""
    client.post("/api/timeslots/bulk", json={
        "start_date": "2026-07-27", "end_date": "2026-08-08",
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
        "periods": [{"period": 1}, {"period": 2}]})


def test_bulk_edit_penalizes_all_of_one_period(client):
    """The 'penalize every period-2 slot' use case: no date range, all
    weekdays, one period."""
    seed_slot_grid(client)
    r = client.post("/api/timeslots/bulk_edit", json={
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "periods": [2], "penalty": 5})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "updated": 12}     # 6 days × 2 weeks
    slots = client.get("/api/timeslots").json()
    assert all(s["penalty"] == (5 if s["period"] == 2 else 0)
               for s in slots)


def test_bulk_edit_penalizes_saturday_period_one(client):
    """'Penalize all Saturday period 1' — weekday × period filter."""
    seed_slot_grid(client)
    r = client.post("/api/timeslots/bulk_edit", json={
        "weekdays": ["Sat"], "periods": [1], "penalty": 9})
    assert r.json()["updated"] == 2                    # two Saturdays
    slots = {s["id"]: s for s in client.get("/api/timeslots").json()}
    assert slots["0801-1"]["penalty"] == 9
    assert slots["0808-1"]["penalty"] == 9
    assert slots["0801-2"]["penalty"] == 0             # other period kept
    assert slots["0727-1"]["penalty"] == 0             # other weekday kept


def test_bulk_edit_respects_date_range_and_keeps_other_fields(client):
    seed_slot_grid(client)
    client.post("/api/timeslots", json={                # give one a label
        "id": "0727-1", "date": "2026-07-27", "period": 1,
        "label": "morning", "penalty": 3})
    r = client.post("/api/timeslots/bulk_edit", json={
        "start_date": "2026-07-27", "end_date": "2026-08-01",
        "weekdays": ["Mon"], "penalty": 7})            # periods [] = all
    assert r.json()["updated"] == 2                    # 0727-1, 0727-2 only
    slots = {s["id"]: s for s in client.get("/api/timeslots").json()}
    assert slots["0727-1"]["penalty"] == 7
    assert slots["0727-1"]["label"] == "morning"       # label untouched
    assert slots["0727-2"]["penalty"] == 7
    assert slots["0803-1"]["penalty"] == 0             # Mon outside range


def test_bulk_edit_sets_label_without_touching_penalty(client):
    seed_slot_grid(client)
    client.post("/api/timeslots/bulk_edit", json={
        "weekdays": ["Sat"], "periods": [1], "penalty": 4})
    r = client.post("/api/timeslots/bulk_edit", json={
        "weekdays": ["Sat"], "periods": [1], "label": "09:00-10:10"})
    assert r.json()["updated"] == 2
    slots = {s["id"]: s for s in client.get("/api/timeslots").json()}
    assert slots["0801-1"]["label"] == "09:00-10:10"
    assert slots["0801-1"]["penalty"] == 4             # penalty untouched


@pytest.mark.parametrize("body,fragment", [
    ({"start_date": "bad", "weekdays": ["Mon"], "penalty": 1},
     "YYYY-MM-DD"),
    ({"start_date": "2026-08-02", "end_date": "2026-08-01",
      "weekdays": ["Mon"], "penalty": 1}, "after"),
    ({"weekdays": ["Monday"], "penalty": 1}, "weekday"),
    ({"weekdays": [], "penalty": 1}, "at least one weekday"),
    ({"weekdays": ["Mon"]}, "penalty and/or a label"),
])
def test_bulk_edit_validation(client, body, fragment):
    seed_slot_grid(client)
    r = client.post("/api/timeslots/bulk_edit", json=body)
    assert r.status_code == 422
    assert fragment in r.json()["detail"]
    slots = client.get("/api/timeslots").json()
    assert all(s["penalty"] == 0 and s["label"] == "" for s in slots)


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
    assert body["timed_out"] is False
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
    assert body["objective"] == {"student_double_days": 0,
                                 "student_day_gaps": 0, "pair_miss": 0,
                                 "slot_spread": 0,
                                 "total_days": 2, "teacher_single_days": 2,
                                 "day_spread": 0, "slot_penalty": 0,
                                 "subject_repeats": 0, "teacher_idle": 0,
                                 "subject_bunching": 0}
    assert body["student_stats"] == [
        {"student_id": "s1", "name": "Aoi", "lessons": 1, "days": 1,
         "double_days": []},
        {"student_id": "s2", "name": "Ren", "lessons": 1, "days": 1,
         "double_days": []}]


def test_schedule_single_day_metric_honors_threshold(client):
    """A two-lesson day is not 'too few' at the default threshold but is
    once single_day_max is raised to 2 via settings."""
    seed_world(client)
    for st, slot in (("s1", "mon-1"), ("s2", "mon-2")):
        client.post("/api/lessons", json={
            "student_id": st, "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1", "timeslot_id": slot})
    assert client.get(
        "/api/schedule").json()["objective"]["teacher_single_days"] == 0
    client.put("/api/settings", json={"single_day_max": 2})
    assert client.get(
        "/api/schedule").json()["objective"]["teacher_single_days"] == 1


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
    # the caller can see what the exact optimizer achieved
    assert body["v2_outcome"] in ("optimal", "improved", "no_improvement")
    sched = client.get("/api/schedule").json()
    assert sched["violations"] == [] and sched["coverage"] == []


def test_generate_v1_has_no_v2_outcome(client):
    seed_world(client)
    client.post("/api/student_needs", json={
        "student_id": "s1", "subject_id": "math", "sessions": 1})
    body = client.post("/api/schedule/generate", json={}).json()
    assert body["v2_outcome"] is None


def test_generate_v2_keeps_existing_schedule_it_cannot_beat(client):
    """The bar to beat is the schedule present when generate is
    clicked: a hand-placed lesson at an unusual slot (mon-2, where a
    fresh solve would pick mon-1) survives a re-generate because no
    schedule is cheaper."""
    pytest.importorskip("ortools")
    seed_world(client)
    client.post("/api/student_needs", json={
        "student_id": "s1", "subject_id": "math", "sessions": 1})
    client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-2"})
    body = client.post("/api/schedule/generate",
                       json={"solver": "v2"}).json()
    assert body["backend"] == "current"
    assert body["v2_outcome"] == "optimal"
    lessons = client.get("/api/schedule").json()["lessons"]
    assert [l["timeslot_id"] for l in lessons] == ["mon-2"]


def test_cancel_generation_mid_solve(client, monkeypatch):
    """POST /api/schedule/cancel stops a running generation; the stored
    schedule is untouched and the response says cancelled."""
    import threading
    import time

    import app.main as m
    from app.scheduler import SolveCancelled
    seed_world(client)
    lid = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"}).json()["id"]

    started = threading.Event()

    def slow_solve(data, fixed_lessons=None, should_stop=None, **kw):
        started.set()
        for _ in range(400):                    # ~20 s unless cancelled
            if should_stop and should_stop():
                raise SolveCancelled()
            time.sleep(0.05)
        raise AssertionError("was never cancelled")

    monkeypatch.setattr(m, "solve", slow_solve)
    out = []
    th = threading.Thread(target=lambda: out.append(
        client.post("/api/schedule/generate", json={})))
    th.start()
    assert started.wait(5), "generation never started"
    r = client.post("/api/schedule/cancel")
    assert r.status_code == 200
    th.join(10)
    assert out and out[0].json()["cancelled"] is True
    # nothing was written or cleared
    lessons = client.get("/api/schedule").json()["lessons"]
    assert [l["id"] for l in lessons] == [lid]


def test_cancel_without_running_generation(client):
    assert client.post("/api/schedule/cancel").status_code == 404


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
    days_first = ["student_double_day", "student_day_gap",
                  "student_teacher_pair",
                  "teacher_working_day", "teacher_single_day",
                  "teacher_slot_spread", "teacher_day_spread",
                  "slot_penalty", "student_subject_repeat",
                  "teacher_idle_gap", "student_subject_spread"]
    r = client.post("/api/schedule/generate",
                    json={"objective_order": days_first})
    assert r.json()["complete"] is True
    body = client.get("/api/schedule").json()
    assert body["objective"]["total_days"] == 1
    teachers = {l["teacher_id"] for l in body["lessons"]}
    assert len(teachers) == 1


# ------------------------------------------------------------------ settings

def test_settings_defaults_and_roundtrip(client):
    from app.scheduler import OBJECTIVE_TERMS
    default_order = list(OBJECTIVE_TERMS)
    assert client.get("/api/settings").json() == {
        "teacher_capacity": 2, "student_day_cap": 2, "single_day_max": 1,
        "objective_caps": {"student_day_gap": 0},
        "objective_order": default_order}
    r = client.put("/api/settings", json={
        "teacher_capacity": 1, "student_day_cap": 3, "single_day_max": 2,
        "objective_caps": {"teacher_slot_spread": 1}})
    assert r.status_code == 200
    assert client.get("/api/settings").json() == {
        "teacher_capacity": 1, "student_day_cap": 3, "single_day_max": 2,
        "objective_caps": {"teacher_slot_spread": 1},
        "objective_order": default_order}


def test_objective_order_persists_and_drives_generate(client):
    """The drag-sorted priority order is a stored setting: it survives
    reloads, tolerates omission on later PUTs, rejects
    non-permutations, and a generate WITHOUT an explicit order uses
    it."""
    from app.scheduler import OBJECTIVE_TERMS
    days_first = ["student_double_day", "student_day_gap",
                  "student_teacher_pair",
                  "teacher_working_day", "teacher_single_day",
                  "teacher_slot_spread", "teacher_day_spread",
                  "slot_penalty", "student_subject_repeat",
                  "teacher_idle_gap", "student_subject_spread"]
    r = client.put("/api/settings", json={"objective_order": days_first})
    assert r.status_code == 200
    assert r.json()["objective_order"] == days_first
    # a PUT without the field keeps the stored order
    client.put("/api/settings", json={"teacher_capacity": 2})
    assert client.get("/api/settings").json()["objective_order"] == \
        days_first
    assert client.put("/api/settings", json={
        "objective_order": ["student_double_day"]}).status_code == 422

    # same scenario as test_generate_honors_objective_order, but the
    # order comes from settings instead of the request
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
    assert client.post("/api/schedule/generate",
                       json={}).json()["complete"] is True
    body = client.get("/api/schedule").json()
    assert body["objective"]["total_days"] == 1
    assert len({l["teacher_id"] for l in body["lessons"]}) == 1
    assert list(OBJECTIVE_TERMS) != days_first   # scenario is meaningful


@pytest.mark.parametrize("body", [
    {"teacher_capacity": 0},
    {"student_day_cap": 9},
    {"single_day_max": 0},
    {"single_day_max": 11},
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


def test_user_sets_one_class_per_day_as_always_active(client):
    """End-to-end user flow: dragging 'One lesson per day per student'
    above the divider stores objective_caps.student_double_day = 0; the
    generated schedule must then have no two-lesson day, and any
    schedule that acquires one is flagged in Status."""
    seed_world(client)
    client.post("/api/student_needs",
                json={"student_id": "s1", "subject_id": "math",
                      "sessions": 2})
    r = client.put("/api/settings",
                   json={"objective_caps": {"student_double_day": 0}})
    assert r.json()["objective_caps"] == {"student_double_day": 0}

    assert client.post("/api/schedule/generate",
                       json={}).json()["complete"] is True
    body = client.get("/api/schedule").json()
    assert body["violations"] == []
    assert body["objective"]["student_double_days"] == 0
    # the two sessions landed on different days
    days = {l["timeslot_id"][:3] for l in body["lessons"]}
    assert len(body["lessons"]) == 2 and len(days) == 2

    # force both onto one day (legal under the base rules: consecutive)
    lid = body["lessons"][0]["id"]
    tue = next(l for l in body["lessons"] if l["timeslot_id"] == "tue-1")
    client.patch(f"/api/lessons/{tue['id']}",
                 json={"timeslot_id": "mon-2", "force": True})
    codes = {v["code"] for v in client.get("/api/schedule").json()["violations"]}
    assert codes == {"objective_cap_exceeded"}
    assert lid  # silence unused warning


ALWAYS_ACTIVE_CASES = [
    # (term, value in the fixture schedule below)
    ("student_double_day", 1),
    ("teacher_slot_spread", 3),
    ("teacher_working_day", 2),
    ("teacher_single_day", 1),
    ("teacher_day_spread", 2),
    # s2 is soft-assigned to t2 (priority 1) but taught by t1: 9 points
    ("student_teacher_pair", 9),
]


@pytest.mark.parametrize("term,value", ALWAYS_ACTIVE_CASES)
def test_any_condition_can_be_always_active(client, term, value):
    """Nothing is hard-coded: EVERY objective term can be set as always
    active through settings, and each is enforced/reported generically.
    Fixture schedule: t1 teaches s1 twice on Mon (consecutive) and s2
    once on Tue; t2 exists, teaches math, but has no lessons; s2 is
    soft-assigned to t2 (priority 1), which the schedule ignores."""
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "math"})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t2", "timeslot_id": "tue-1"})
    client.post("/api/teacher_students",
                json={"teacher_id": "t2", "student_id": "s2",
                      "priority": 1})
    for st, slot in (("s1", "mon-1"), ("s1", "mon-2"), ("s2", "tue-1")):
        assert client.post("/api/lessons", json={
            "student_id": st, "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1", "timeslot_id": slot}).status_code == 200
    assert client.get("/api/schedule").json()["violations"] == []

    # cap just below the schedule's value -> violation reported
    client.put("/api/settings", json={"objective_caps": {term: value - 1}})
    vs = client.get("/api/schedule").json()["violations"]
    assert {v["code"] for v in vs} == {"objective_cap_exceeded"}, term
    # cap at the value -> clean again
    client.put("/api/settings", json={"objective_caps": {term: value}})
    assert client.get("/api/schedule").json()["violations"] == []


def test_undo_grid_edits_in_reverse_order(client):
    """Availability edits join the same undo stack, stored as inverse
    deltas: a bulk block edit and a later single toggle are undone in
    reverse order, without touching unrelated cells."""
    seed_world(client)
    client.post("/api/student_availability/bulk", json={
        "add": [], "remove": [["s1", "mon-1"], ["s1", "mon-2"],
                              ["s2", "mon-1"], ["s2", "mon-2"]]})
    client.delete(
        "/api/student_availability?student_id=s2&timeslot_id=tue-1")
    assert client.get("/api/undo").json()["label"] == \
        "availability change"

    def on():
        return {(a["student_id"], a["timeslot_id"]) for a in
                client.get("/api/student_availability").json()}

    assert client.post("/api/schedule/undo").json()["undid"] == \
        "availability change"
    now = on()
    assert ("s2", "tue-1") in now          # single toggle reverted
    assert ("s1", "mon-1") not in now      # block edit still applied
    assert client.post("/api/schedule/undo").json()["undid"] == \
        "availability block edit"
    now = on()
    assert {("s1", "mon-1"), ("s1", "mon-2"),
            ("s2", "mon-1"), ("s2", "mon-2")} <= now

    # a no-op (adding an already-available cell) pushes nothing
    n = client.get("/api/undo").json()["count"]
    client.post("/api/student_availability",
                json={"student_id": "s1", "timeslot_id": "mon-1"})
    assert client.get("/api/undo").json()["count"] == n


def test_undo_assignment_edits_restores_priorities(client):
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_students", json={
        "teacher_id": "t1", "student_id": "s1", "priority": 2})
    client.post("/api/teacher_students/bulk", json={
        "set": [["t1", "s1", 0], ["t2", "s2", 1]], "clear": []})

    def pairs():
        return {(p["teacher_id"], p["student_id"]): p["priority"]
                for p in client.get("/api/teacher_students").json()}

    assert pairs() == {("t1", "s1"): 0, ("t2", "s2"): 1}
    # undo the bulk: t1-s1 back to its PRIOR priority 2, t2-s2 gone
    assert client.post("/api/schedule/undo").json()["undid"] == \
        "assignment block edit"
    assert pairs() == {("t1", "s1"): 2}
    # undo the single add
    assert client.post("/api/schedule/undo").json()["undid"] == \
        "assignment change"
    assert pairs() == {}


def test_bulk_availability_set_and_clear(client):
    """One transaction flips a whole rectangle of availability cells
    (used by area select / paste in the Availability tab)."""
    seed_world(client)
    r = client.post("/api/student_availability/bulk", json={
        "add": [], "remove": [["s1", "mon-1"], ["s1", "mon-2"],
                              ["s2", "mon-1"]]})
    assert r.status_code == 200 and r.json()["removed"] == 3
    left = {(a["student_id"], a["timeslot_id"])
            for a in client.get("/api/student_availability").json()}
    assert ("s1", "mon-1") not in left and ("s2", "tue-1") in left
    # add is idempotent (INSERT OR REPLACE)
    r = client.post("/api/student_availability/bulk", json={
        "add": [["s1", "mon-1"], ["s1", "mon-1"], ["s2", "mon-1"]],
        "remove": []})
    assert r.status_code == 200
    left = {(a["student_id"], a["timeslot_id"])
            for a in client.get("/api/student_availability").json()}
    assert ("s1", "mon-1") in left and ("s2", "mon-1") in left
    # unknown references and malformed pairs are 422, nothing changes
    assert client.post("/api/teacher_availability/bulk", json={
        "add": [["ghost", "mon-1"]], "remove": []}).status_code == 422
    assert client.post("/api/teacher_availability/bulk", json={
        "add": [["t1"]], "remove": []}).status_code == 422


def test_bulk_teacher_students_set_and_clear(client):
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    r = client.post("/api/teacher_students/bulk", json={
        "set": [["t1", "s1", 0], ["t2", "s1", 2], ["t2", "s2", 1]],
        "clear": []})
    assert r.status_code == 200 and r.json()["set"] == 3
    pairs = {(p["teacher_id"], p["student_id"]): p["priority"]
             for p in client.get("/api/teacher_students").json()}
    assert pairs == {("t1", "s1"): 0, ("t2", "s1"): 2, ("t2", "s2"): 1}
    r = client.post("/api/teacher_students/bulk", json={
        "set": [["t2", "s1", 3]], "clear": [["t1", "s1"]]})
    assert r.status_code == 200
    pairs = {(p["teacher_id"], p["student_id"]): p["priority"]
             for p in client.get("/api/teacher_students").json()}
    assert pairs == {("t2", "s1"): 3, ("t2", "s2"): 1}
    assert client.post("/api/teacher_students/bulk", json={
        "set": [["t1", "s1", 10]], "clear": []}).status_code == 422
    assert client.post("/api/teacher_students/bulk", json={
        "set": [], "clear": [["t1"]]}).status_code == 422


def test_teacher_students_csv_roundtrip(client):
    """Assignments export/import through the CSV tab endpoints,
    priorities included; import replaces the table's contents."""
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_students", json={
        "teacher_id": "t1", "student_id": "s1", "priority": 0})
    client.post("/api/teacher_students", json={
        "teacher_id": "t2", "student_id": "s2", "priority": 3})
    r = client.get("/api/export/teacher_students")
    assert r.status_code == 200
    assert r.text == ("teacher_id,student_id,priority\n"
                      "t1,s1,0\nt2,s2,3\n")
    # import a different set: it becomes the table's full contents
    csv = "teacher_id,student_id,priority\nt2,s1,1\n"
    r = client.post("/api/import/teacher_students",
                    files={"file": ("a.csv", csv, "text/csv")})
    assert r.status_code == 200 and r.json()["rows"] == 1
    assert client.get("/api/teacher_students").json() == [
        {"teacher_id": "t2", "student_id": "s1", "priority": 1}]
    # invalid priority: all-or-nothing, nothing changes
    bad = "teacher_id,student_id,priority\nt1,s1,10\n"
    assert client.post("/api/import/teacher_students",
                       files={"file": ("b.csv", bad, "text/csv")}
                       ).status_code == 422
    assert len(client.get("/api/teacher_students").json()) == 1


def test_teacher_students_crud_and_hard_pair_flow(client):
    """The Assignments tab's API: upsert with priority, listing, delete;
    a priority-0 pair blocks manual adds by other teachers through the
    usual caution flow."""
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "math"})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t2", "timeslot_id": "mon-1"})
    r = client.post("/api/teacher_students", json={
        "teacher_id": "t2", "student_id": "s1", "priority": 0})
    assert r.status_code == 200
    assert client.get("/api/teacher_students").json() == [
        {"teacher_id": "t2", "student_id": "s1", "priority": 0}]
    # upsert changes the priority in place
    client.post("/api/teacher_students", json={
        "teacher_id": "t2", "student_id": "s1", "priority": 2})
    assert client.get("/api/teacher_students").json()[0]["priority"] == 2
    assert client.post("/api/teacher_students", json={
        "teacher_id": "t2", "student_id": "s1", "priority": 10}
        ).status_code == 422
    assert client.post("/api/teacher_students", json={
        "teacher_id": "ghost", "student_id": "s1", "priority": 1}
        ).status_code == 422

    # back to hard: s1 must now be taught by t2
    client.post("/api/teacher_students", json={
        "teacher_id": "t2", "student_id": "s1", "priority": 0})
    base = {"student_id": "s1", "subject_id": "math", "room_id": "r1",
            "timeslot_id": "mon-1"}
    r = client.post("/api/lessons", json=dict(base, teacher_id="t1"))
    assert r.status_code == 409
    assert any(v["code"] == "student_teacher_mismatch"
               for v in r.json()["detail"]["violations"])
    assert client.post("/api/lessons", json=dict(
        base, teacher_id="t2")).status_code == 200

    client.delete("/api/teacher_students?teacher_id=t2&student_id=s1")
    assert client.get("/api/teacher_students").json() == []


def test_generate_respects_hard_pair(client):
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "math"})
    for slot in ("mon-1", "mon-2", "tue-1"):
        client.post("/api/teacher_availability",
                    json={"teacher_id": "t2", "timeslot_id": slot})
    client.post("/api/teacher_students", json={
        "teacher_id": "t2", "student_id": "s1", "priority": 0})
    client.post("/api/student_needs", json={
        "student_id": "s1", "subject_id": "math", "sessions": 2})
    assert client.post("/api/schedule/generate",
                       json={}).json()["complete"] is True
    body = client.get("/api/schedule").json()
    assert body["violations"] == []
    assert {l["teacher_id"] for l in body["lessons"]} == {"t2"}


def test_room_teacher_limit_via_api(client):
    """Rooms carry an optional teacher limit; adding a lesson that would
    put a second teacher into the room-slot is cautioned like any other
    violation and can be forced."""
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "math"})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t2", "timeslot_id": "mon-1"})
    client.post("/api/rooms",
                json={"id": "r2", "name": "Hall", "capacity": 2,
                      "teacher_capacity": 1})
    rooms = {r["id"]: r for r in client.get("/api/rooms").json()}
    assert rooms["r2"]["teacher_capacity"] == 1
    assert rooms["r1"]["teacher_capacity"] == 0     # default: no limit

    base = {"subject_id": "math", "room_id": "r2", "timeslot_id": "mon-1"}
    assert client.post("/api/lessons", json=dict(
        base, student_id="s1", teacher_id="t1")).status_code == 200
    r = client.post("/api/lessons", json=dict(
        base, student_id="s2", teacher_id="t2"))
    assert r.status_code == 409
    assert any(v["code"] == "room_teacher_over_capacity"
               for v in r.json()["detail"]["violations"])
    r = client.post("/api/lessons", json=dict(
        base, student_id="s2", teacher_id="t2", force=True))
    assert r.status_code == 200
    codes = [v["code"] for v in
             client.get("/api/schedule").json()["violations"]]
    assert "room_teacher_over_capacity" in codes


def test_teacher_day_max_via_api(client):
    """Teachers carry an optional daily lesson cap; the caution flow
    treats a breach like any other violation, and renaming a teacher
    without sending the field keeps the stored limit."""
    seed_world(client)
    client.post("/api/teachers", json={
        "id": "t1", "name": "Tanaka", "max_lessons_per_day": 1})
    rows = {t["id"]: t for t in client.get("/api/teachers").json()}
    assert rows["t1"]["max_lessons_per_day"] == 1

    base = {"subject_id": "math", "teacher_id": "t1", "room_id": "r1"}
    assert client.post("/api/lessons", json=dict(
        base, student_id="s1", timeslot_id="mon-1")).status_code == 200
    r = client.post("/api/lessons", json=dict(
        base, student_id="s2", timeslot_id="mon-2"))
    assert r.status_code == 409
    assert any(v["code"] == "teacher_day_overload"
               for v in r.json()["detail"]["violations"])
    r = client.post("/api/lessons", json=dict(
        base, student_id="s2", timeslot_id="mon-2", force=True))
    assert r.status_code == 200
    codes = [v["code"] for v in
             client.get("/api/schedule").json()["violations"]]
    assert "teacher_day_overload" in codes

    # rename without the field: the limit must survive
    client.post("/api/teachers", json={"id": "t1", "name": "Tanaka K."})
    rows = {t["id"]: t for t in client.get("/api/teachers").json()}
    assert rows["t1"] == {"id": "t1", "name": "Tanaka K.",
                          "max_lessons_per_day": 1}


def seed_two_weeks(client):
    """seed_world plus the same slots one week later (Aug 3/4)."""
    seed_world(client)
    for sid, date, period in [("mon2-1", "2026-08-03", 1),
                              ("mon2-2", "2026-08-03", 2),
                              ("tue2-1", "2026-08-04", 1)]:
        client.post("/api/timeslots",
                    json={"id": sid, "date": date, "period": period})
        client.post("/api/teacher_availability",
                    json={"teacher_id": "t1", "timeslot_id": sid})
        for st in ("s1", "s2"):
            client.post("/api/student_availability",
                        json={"student_id": st, "timeslot_id": sid})


def test_repeat_lessons_over_following_weeks(client):
    """Selected lessons are stamped onto the same weekday+period of the
    next N weeks; weeks without a matching slot are skipped, duplicates
    are not re-created."""
    seed_two_weeks(client)
    ids = []
    for slot in ("mon-1", "tue-1"):
        r = client.post("/api/lessons", json={
            "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1", "timeslot_id": slot})
        ids.append(r.json()["id"])

    r = client.post("/api/lessons/repeat",
                    json={"lesson_ids": ids, "weeks": 2})
    assert r.status_code == 200
    body = r.json()
    # week+1 exists for both; week+2 slots don't exist
    assert body["created"] == 2
    assert body["skipped_no_slot"] == 2
    slots = sorted(l["timeslot_id"] for l in
                   client.get("/api/schedule").json()["lessons"])
    assert slots == ["mon-1", "mon2-1", "tue-1", "tue2-1"]

    # running it again creates nothing new
    r = client.post("/api/lessons/repeat",
                    json={"lesson_ids": ids, "weeks": 1}).json()
    assert (r["created"], r["skipped_duplicate"]) == (0, 2)


def test_repeat_conflicts_use_caution_flow(client):
    """A copy that collides with an existing lesson is 409 unless
    forced, exactly like a manual add."""
    seed_two_weeks(client)
    lid = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"}).json()["id"]
    # occupy the target: s1 already busy at mon2-1
    client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon2-2"})
    client.post("/api/lessons", json={
        "student_id": "s2", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon2-1"})   # room r1 is full
    r = client.post("/api/lessons/repeat",
                    json={"lesson_ids": [lid], "weeks": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["violations"]
    r = client.post("/api/lessons/repeat",
                    json={"lesson_ids": [lid], "weeks": 1, "force": True})
    assert r.status_code == 200
    assert r.json()["created"] == 1
    assert r.json()["violations"]


def test_undo_reverts_manual_edits_in_order(client):
    """Undo walks back manual edits one at a time: bulk edit, then
    delete, then a move — restoring ids and fields exactly."""
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "math"})
    for slot in ("mon-1", "mon-2", "tue-1"):
        client.post("/api/teacher_availability",
                    json={"teacher_id": "t2", "timeslot_id": slot})
    lid = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"}).json()["id"]

    def snap():
        return {(l["id"], l["teacher_id"], l["timeslot_id"]) for l in
                client.get("/api/schedule").json()["lessons"]}

    after_add = snap()
    client.patch(f"/api/lessons/{lid}", json={"timeslot_id": "tue-1"})
    after_move = snap()
    client.post("/api/lessons/bulk_update",
                json={"lesson_ids": [lid], "teacher_id": "t2"})
    client.delete(f"/api/lessons/{lid}")
    assert snap() == set()

    undo = client.get("/api/schedule").json()["undo"]
    assert undo["count"] >= 4 and undo["label"] == "delete lesson"
    assert client.post("/api/schedule/undo").json()["undid"] == \
        "delete lesson"
    assert snap() == {(lid, "t2", "tue-1")}
    assert client.post("/api/schedule/undo").json()["undid"] == \
        "edit selected lessons"
    assert snap() == after_move
    assert client.post("/api/schedule/undo").json()["undid"] == \
        "move/edit lesson"
    assert snap() == after_add
    assert client.post("/api/schedule/undo").json()["undid"] == \
        "add lesson"
    assert snap() == set()
    # below the lesson edits sit the seeding's availability entries
    assert client.post("/api/schedule/undo").json()["undid"] == \
        "availability change"


def test_undo_covers_repeat_and_preserves_locks(client):
    seed_two_weeks(client)
    lid = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"}).json()["id"]
    client.post(f"/api/lessons/{lid}/lock", json={"locked": True})
    client.post("/api/lessons/repeat",
                json={"lesson_ids": [lid], "weeks": 1})
    assert len(client.get("/api/schedule").json()["lessons"]) == 2
    assert client.post("/api/schedule/undo").json()["undid"] == \
        "repeat lessons"
    lessons = client.get("/api/schedule").json()["lessons"]
    assert [(l["id"], l["locked"]) for l in lessons] == [(lid, True)]


def test_generate_and_clear_reset_undo_history(client):
    seed_world(client)
    base = client.get("/api/undo").json()["count"]
    client.post("/api/student_needs", json={
        "student_id": "s1", "subject_id": "math", "sessions": 1})
    client.post("/api/lessons", json={
        "student_id": "s2", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-2"})
    assert client.get("/api/schedule").json()["undo"]["count"] == base + 1
    client.post("/api/schedule/generate", json={})
    assert client.get("/api/schedule").json()["undo"]["count"] == 0
    assert client.post("/api/schedule/undo").status_code == 404
    client.post("/api/lessons", json={
        "student_id": "s2", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-2", "force": True})
    assert client.get("/api/schedule").json()["undo"]["count"] == 1
    client.delete("/api/schedule")
    assert client.get("/api/schedule").json()["undo"]["count"] == 0


def test_undo_with_vanished_references_fails_cleanly(client):
    """If the snapshot references since-deleted master data, undo says
    so once and drops the stale entry."""
    seed_world(client)
    client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    client.patch("/api/lessons/1", json={"timeslot_id": "mon-2"})
    client.delete("/api/students/s1")      # cascades the lesson away
    before = client.get("/api/undo").json()["count"]
    r = client.post("/api/schedule/undo")  # snapshot references s1
    assert r.status_code == 409
    assert "changed" in r.json()["detail"]
    # the stale entry is gone; the next one may fail the same way but
    # the stack never gets stuck
    assert client.get("/api/undo").json()["count"] == before - 1


def test_bulk_update_changes_selected_lessons(client):
    """Teacher/subject/room apply to all given lessons at once; locked
    lessons are skipped and reported."""
    seed_world(client)
    client.post("/api/teachers", json={"id": "t2", "name": "Suzuki"})
    client.post("/api/teacher_subjects",
                json={"teacher_id": "t2", "subject_id": "math"})
    for slot in ("mon-1", "mon-2", "tue-1"):
        client.post("/api/teacher_availability",
                    json={"teacher_id": "t2", "timeslot_id": slot})
    ids = []
    for st, slot in (("s1", "mon-1"), ("s2", "tue-1")):
        r = client.post("/api/lessons", json={
            "student_id": st, "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1", "timeslot_id": slot})
        ids.append(r.json()["id"])
    client.post(f"/api/lessons/{ids[1]}/lock", json={"locked": True})

    r = client.post("/api/lessons/bulk_update",
                    json={"lesson_ids": ids, "teacher_id": "t2"})
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    assert r.json()["skipped_locked"] == 1
    teachers = {l["id"]: l["teacher_id"] for l in
                client.get("/api/schedule").json()["lessons"]}
    assert teachers[ids[0]] == "t2"
    assert teachers[ids[1]] == "t1"        # locked one untouched


def test_bulk_update_conflicts_use_caution_flow(client):
    """An update that makes a lesson invalid (teacher can't teach the
    new subject) is 409 unless forced."""
    seed_world(client)
    client.post("/api/subjects", json={"id": "eng", "name": "English"})
    lid = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"}).json()["id"]
    r = client.post("/api/lessons/bulk_update",
                    json={"lesson_ids": [lid], "subject_id": "eng"})
    assert r.status_code == 409
    assert r.json()["detail"]["violations"]
    r = client.post("/api/lessons/bulk_update",
                    json={"lesson_ids": [lid], "subject_id": "eng",
                          "force": True})
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    assert r.json()["violations"]


def test_bulk_update_validates_input(client):
    seed_world(client)
    lid = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"}).json()["id"]
    assert client.post("/api/lessons/bulk_update",
                       json={"lesson_ids": [lid]}).status_code == 422
    assert client.post("/api/lessons/bulk_update",
                       json={"lesson_ids": [lid], "teacher_id": "ghost"}
                       ).status_code == 422
    assert client.post("/api/lessons/bulk_update",
                       json={"lesson_ids": [999], "teacher_id": "t1"}
                       ).status_code == 404


def test_paste_lessons_anchored_at_target(client):
    """Paste maps the earliest copied lesson onto the target slot and
    shifts the rest by the same day/period offset; missing slots and
    duplicates are skipped, locks are copied along."""
    seed_two_weeks(client)
    clip = []
    for st, slot in (("s1", "mon-1"), ("s2", "mon-2")):
        lid = client.post("/api/lessons", json={
            "student_id": st, "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1", "timeslot_id": slot}).json()["id"]
        clip.append({"student_id": st, "subject_id": "math",
                     "teacher_id": "t1", "room_id": "r1",
                     "timeslot_id": slot, "locked": st == "s1"})
        if st == "s1":
            client.post(f"/api/lessons/{lid}/lock", json={"locked": True})

    # target = next Monday P1: both P1 and P2 exist there -> 2 created
    r = client.post("/api/lessons/paste", json={
        "lessons": clip, "target_timeslot_id": "mon2-1"})
    assert r.status_code == 200
    assert r.json()["created"] == 2
    lessons = client.get("/api/schedule").json()["lessons"]
    pasted = {l["timeslot_id"]: l for l in lessons
              if l["timeslot_id"].startswith("mon2")}
    assert set(pasted) == {"mon2-1", "mon2-2"}
    assert pasted["mon2-1"]["locked"] is True     # lock copied
    assert pasted["mon2-2"]["locked"] is False

    # target = tue-1 (P1): the P2 companion has no tue-2 slot
    r = client.post("/api/lessons/paste", json={
        "lessons": clip, "target_timeslot_id": "tue-1"}).json()
    assert (r["created"], r["skipped_no_slot"]) == (1, 1)
    # pasting the same block again -> duplicates skipped
    r = client.post("/api/lessons/paste", json={
        "lessons": clip, "target_timeslot_id": "tue-1"}).json()
    assert (r["created"], r["skipped_duplicate"]) == (0, 1)

    # undo reverts the last paste
    assert client.post("/api/schedule/undo").json()["undid"] == \
        "paste lessons"

    assert client.post("/api/lessons/paste", json={
        "lessons": clip, "target_timeslot_id": "ghost"}
        ).status_code == 404
    bad = [dict(clip[0], teacher_id="ghost")]
    assert client.post("/api/lessons/paste", json={
        "lessons": bad, "target_timeslot_id": "mon-1"}
        ).status_code == 422


def test_bulk_lock_and_repeat_inherits_lock(client):
    """bulk_lock flips several lessons in one call, and repeat copies
    now inherit the source's lock status."""
    seed_two_weeks(client)
    ids = []
    for st, slot in (("s1", "mon-1"), ("s2", "mon-2")):
        ids.append(client.post("/api/lessons", json={
            "student_id": st, "subject_id": "math", "teacher_id": "t1",
            "room_id": "r1", "timeslot_id": slot}).json()["id"])
    r = client.post("/api/lessons/bulk_lock",
                    json={"lesson_ids": ids, "locked": True})
    assert r.status_code == 200 and r.json()["count"] == 2
    flags = {l["id"]: l["locked"] for l in
             client.get("/api/schedule").json()["lessons"]}
    assert all(flags[i] for i in ids)
    assert client.post("/api/lessons/bulk_lock",
                       json={"lesson_ids": [999], "locked": True}
                       ).status_code == 404

    # repeat: the locked sources produce locked copies
    r = client.post("/api/lessons/repeat",
                    json={"lesson_ids": ids, "weeks": 1})
    assert r.json()["created"] == 2
    lessons = client.get("/api/schedule").json()["lessons"]
    copies = [l for l in lessons if l["id"] not in ids]
    assert len(copies) == 2 and all(l["locked"] for l in copies)

    # unlock everything again in one call
    all_ids = [l["id"] for l in lessons]
    client.post("/api/lessons/bulk_lock",
                json={"lesson_ids": all_ids, "locked": False})
    assert not any(l["locked"] for l in
                   client.get("/api/schedule").json()["lessons"])


def test_repeat_validates_input(client):
    seed_world(client)
    assert client.post("/api/lessons/repeat",
                       json={"lesson_ids": [999], "weeks": 1}
                       ).status_code == 404
    assert client.post("/api/lessons/repeat",
                       json={"lesson_ids": [], "weeks": 1}
                       ).status_code == 422
    assert client.post("/api/lessons/repeat",
                       json={"lesson_ids": [1], "weeks": 0}
                       ).status_code == 422


def test_lesson_lock_guards_moves_edits_and_deletion(client):
    """A locked lesson refuses PATCH/DELETE (409) until unlocked, and
    survives Clear schedule."""
    seed_world(client)
    r = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"})
    lid = r.json()["id"]
    client.post("/api/lessons", json={
        "student_id": "s2", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-2"})

    assert client.post(f"/api/lessons/{lid}/lock",
                       json={"locked": True}).status_code == 200
    sched = client.get("/api/schedule").json()
    locked_flags = {l["id"]: l["locked"] for l in sched["lessons"]}
    assert locked_flags[lid] is True

    assert client.patch(f"/api/lessons/{lid}",
                        json={"timeslot_id": "tue-1"}).status_code == 409
    assert client.delete(f"/api/lessons/{lid}").status_code == 409

    r = client.delete("/api/schedule")     # clear keeps the locked one
    assert r.json() == {"ok": True, "deleted": 1, "kept_locked": 1}
    remaining = client.get("/api/schedule").json()["lessons"]
    assert [l["id"] for l in remaining] == [lid]

    client.post(f"/api/lessons/{lid}/lock", json={"locked": False})
    assert client.delete(f"/api/lessons/{lid}").status_code == 200
    assert client.post("/api/lessons/999/lock",
                       json={"locked": True}).status_code == 404


def test_generate_pins_locked_lessons_without_keep_existing(client):
    """A locked lesson at an unusual slot survives a full re-generate
    (keep_existing off) with its lock intact; unlocked lessons are
    re-solved as usual."""
    seed_world(client)
    client.post("/api/student_needs", json={
        "student_id": "s1", "subject_id": "math", "sessions": 1})
    r = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-2"})
    client.post(f"/api/lessons/{r.json()['id']}/lock",
                json={"locked": True})
    assert client.post("/api/schedule/generate",
                       json={}).json()["complete"] is True
    lessons = client.get("/api/schedule").json()["lessons"]
    assert [(l["timeslot_id"], l["locked"]) for l in lessons] == \
        [("mon-2", True)]


def test_backup_download_and_restore_roundtrip(client):
    """Download a consistent snapshot, keep editing, then restore it —
    the schedule (locks included) comes back exactly."""
    seed_world(client)
    lid = client.post("/api/lessons", json={
        "student_id": "s1", "subject_id": "math", "teacher_id": "t1",
        "room_id": "r1", "timeslot_id": "mon-1"}).json()["id"]
    client.post(f"/api/lessons/{lid}/lock", json={"locked": True})

    r = client.get("/api/backup.db")
    assert r.status_code == 200
    assert r.content.startswith(b"SQLite format 3\x00")
    assert "school-backup-" in r.headers["content-disposition"]
    backup = r.content

    # diverge: unlock + delete the lesson, add another student
    client.post(f"/api/lessons/{lid}/lock", json={"locked": False})
    client.delete(f"/api/lessons/{lid}")
    client.post("/api/students", json={"id": "s9", "name": "Ghost"})
    assert client.get("/api/schedule").json()["lessons"] == []

    r = client.post("/api/backup.db",
                    files={"file": ("backup.db", backup,
                                    "application/x-sqlite3")})
    assert r.status_code == 200
    assert r.json()["lessons"] == 1
    lessons = client.get("/api/schedule").json()["lessons"]
    assert [(l["id"], l["timeslot_id"], l["locked"]) for l in lessons] \
        == [(lid, "mon-1", True)]
    assert not any(s["id"] == "s9"
                   for s in client.get("/api/students").json())


def test_restore_rejects_bad_files(client, tmp_path):
    seed_world(client)
    r = client.post("/api/backup.db",
                    files={"file": ("x.db", b"not a database", "x")})
    assert r.status_code == 422
    # a valid SQLite file that is NOT a scheduler database
    import sqlite3 as s3
    other = tmp_path / "other.db"
    conn = s3.connect(other)
    with conn:
        conn.execute("CREATE TABLE misc (x)")
    conn.close()
    r = client.post("/api/backup.db",
                    files={"file": ("x.db", other.read_bytes(), "x")})
    assert r.status_code == 422
    assert "missing tables" in r.json()["detail"]
    # the live data survived both rejections
    assert client.get("/api/students").json()


def test_restore_migrates_old_backups(tmp_path):
    """A backup from before newer columns existed restores cleanly:
    the schema migrations run right after the file is swapped in."""
    from app import db as appdb
    old = tmp_path / "old-backup.db"
    appdb.init_db(old)
    conn = appdb.connect(old)
    with conn:
        conn.execute("INSERT INTO students VALUES ('s1', 'Aoi')")
        # simulate an old backup: drop a recently-added column's table
        # row instead — easiest realistic case: strip lessons.locked
        conn.execute("ALTER TABLE lessons DROP COLUMN locked")
    conn.close()
    data = old.read_bytes()

    app.state.db_path = tmp_path / "live.db"
    try:
        with TestClient(app) as c:
            c.post("/api/students", json={"id": "sX", "name": "Gone"})
            r = c.post("/api/backup.db",
                       files={"file": ("old.db", data, "x")})
            assert r.status_code == 200
            # data restored; the dropped 'locked' column was re-added by
            # the migration (loading the schedule reads it)
            assert c.get("/api/schedule").json()["lessons"] == []
            students = c.get("/api/students").json()
            assert [s["id"] for s in students] == ["s1"]
    finally:
        del app.state.db_path


def test_old_db_gains_locked_lessons_column(tmp_path):
    """Lessons tables from before the lock feature are migrated in
    place on startup."""
    import sqlite3 as s3
    from app import db as appdb
    db_path = tmp_path / "old_lessons.db"
    conn = s3.connect(db_path)
    with conn:
        conn.execute("CREATE TABLE lessons (id INTEGER PRIMARY KEY, "
                     "student_id TEXT, subject_id TEXT, teacher_id TEXT, "
                     "room_id TEXT, timeslot_id TEXT)")
        conn.execute("INSERT INTO lessons VALUES "
                     "(1, 's1', 'math', 't1', 'r1', 'mon-1')")
    conn.close()
    appdb.init_db(db_path)
    conn = appdb.connect(db_path)
    row = conn.execute("SELECT locked FROM lessons WHERE id = 1").fetchone()
    conn.close()
    assert row["locked"] == 0


def test_old_db_gains_teacher_day_max_column(tmp_path):
    """Teachers tables from before the daily cap existed are migrated
    in place on startup (ALTER TABLE, default 0 = no limit)."""
    import sqlite3 as s3
    from app import db as appdb
    db_path = tmp_path / "old_teachers.db"
    conn = s3.connect(db_path)
    with conn:
        conn.execute("CREATE TABLE teachers (id TEXT PRIMARY KEY, "
                     "name TEXT NOT NULL)")
        conn.execute("INSERT INTO teachers VALUES ('t1', 'Tanaka')")
    conn.close()
    appdb.init_db(db_path)
    conn = appdb.connect(db_path)
    row = conn.execute("SELECT max_lessons_per_day FROM teachers "
                       "WHERE id = 't1'").fetchone()
    conn.close()
    assert row["max_lessons_per_day"] == 0


def test_old_db_gains_room_teacher_capacity_column(tmp_path):
    """DBs created before the room teacher limit existed are migrated
    in place on startup (ALTER TABLE, default 0 = no limit)."""
    import sqlite3 as s3
    from app import db as appdb
    db_path = tmp_path / "old.db"
    conn = s3.connect(db_path)
    with conn:
        conn.execute("CREATE TABLE rooms (id TEXT PRIMARY KEY, "
                     "name TEXT NOT NULL, capacity INTEGER NOT NULL)")
        conn.execute("INSERT INTO rooms VALUES ('r1', 'Room 1', 3)")
    conn.close()
    appdb.init_db(db_path)
    conn = appdb.connect(db_path)
    row = conn.execute("SELECT capacity, teacher_capacity FROM rooms "
                       "WHERE id = 'r1'").fetchone()
    conn.close()
    assert (row["capacity"], row["teacher_capacity"]) == (3, 0)


def test_old_db_gains_timeslot_penalty_column(tmp_path):
    """DBs (and hence old backups) from before slot penalties existed
    are migrated in place on startup (ALTER TABLE, default 0)."""
    import sqlite3 as s3
    from app import db as appdb
    db_path = tmp_path / "old_slots.db"
    conn = s3.connect(db_path)
    with conn:
        conn.execute("CREATE TABLE timeslots (id TEXT PRIMARY KEY, "
                     "date TEXT NOT NULL, period INTEGER NOT NULL, "
                     "label TEXT NOT NULL DEFAULT '', UNIQUE (date, period))")
        conn.execute("INSERT INTO timeslots VALUES "
                     "('mon-1', '2026-07-27', 1, '')")
    conn.close()
    appdb.init_db(db_path)
    conn = appdb.connect(db_path)
    row = conn.execute("SELECT penalty FROM timeslots "
                       "WHERE id = 'mon-1'").fetchone()
    conn.close()
    assert row["penalty"] == 0


def test_timeslot_penalty_kept_when_post_omits_it(client):
    """Edits that do not send the penalty field (e.g. old clients or a
    label-only update) must not reset a stored penalty."""
    client.post("/api/timeslots", json={
        "id": "x-1", "date": "2026-07-27", "period": 1, "penalty": 5})
    client.post("/api/timeslots", json={
        "id": "x-1", "date": "2026-07-27", "period": 1, "label": "17:00"})
    rows = client.get("/api/timeslots").json()
    assert rows == [{"id": "x-1", "date": "2026-07-27", "period": 1,
                     "label": "17:00", "penalty": 5}]
    # an explicit value still updates it
    client.post("/api/timeslots", json={
        "id": "x-1", "date": "2026-07-27", "period": 1, "label": "17:00",
        "penalty": 0})
    assert client.get("/api/timeslots").json()[0]["penalty"] == 0


@pytest.mark.parametrize("legacy,expect_gap_cap", [("1", True), ("0", False)])
def test_legacy_require_consecutive_migrates_once(tmp_path, legacy,
                                                  expect_gap_cap):
    """DBs written before the consecutiveness rule became a draggable
    condition stored a `require_consecutive` boolean. On startup its
    intent folds into objective_caps (keeping existing caps) and the
    legacy row is deleted, so a later demotion is never re-overridden."""
    from app import db as appdb
    db_path = tmp_path / "legacy.db"
    appdb.init_db(db_path)
    conn = appdb.connect(db_path)
    with conn:
        conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", [
            ("require_consecutive", legacy),
            ("objective_caps", '{"student_double_day": 0}')])
    conn.close()
    app.state.db_path = db_path
    try:
        with TestClient(app) as c:
            caps = c.get("/api/settings").json()["objective_caps"]
            expected = {"student_double_day": 0}
            if expect_gap_cap:
                expected["student_day_gap"] = 0
            assert caps == expected
            # demote, then "restart": the migration must not resurrect it
            c.put("/api/settings", json={"objective_caps": {}})
        with TestClient(app) as c:
            assert c.get("/api/settings").json()["objective_caps"] == {}
    finally:
        del app.state.db_path


def test_consecutiveness_is_a_demotable_condition(client):
    """'Multiple lessons on a day must be consecutive' is a draggable
    condition like any other. s1 is only free Mon P1 and Mon P3, and
    needs two sessions — a consecutive pair is impossible.

    While the condition is always active (the default: cap 0) the
    schedule stays incomplete. Demoting it below the divider (removing
    the cap) lets the gap through: schedule complete, no violations, and
    the gap is COUNTED in the objective metrics instead."""
    seed_world(client)
    client.post("/api/timeslots", json={"id": "mon-3", "date": "2026-07-27",
                                        "period": 3})
    client.post("/api/teacher_availability",
                json={"teacher_id": "t1", "timeslot_id": "mon-3"})
    client.post("/api/student_availability",
                json={"student_id": "s1", "timeslot_id": "mon-3"})
    # s1: only Mon P1 and Mon P3 remain
    for slot in ("mon-2", "tue-1"):
        client.delete(f"/api/student_availability?student_id=s1"
                      f"&timeslot_id={slot}")
    client.post("/api/student_needs",
                json={"student_id": "s1", "subject_id": "math",
                      "sessions": 2})

    # default settings: the condition is always active -> incomplete
    assert client.get("/api/settings").json()["objective_caps"] == \
        {"student_day_gap": 0}
    body = client.post("/api/schedule/generate", json={}).json()
    assert body["complete"] is False

    # demote the condition (what dragging it below the divider does)
    client.put("/api/settings", json={"objective_caps": {}})
    body = client.post("/api/schedule/generate", json={}).json()
    assert body["complete"] is True
    sched = client.get("/api/schedule").json()
    assert sched["violations"] == []                 # gap is legal now
    assert sched["objective"]["student_day_gaps"] == 1   # ...but visible
    periods = sorted(l["timeslot_id"] for l in sched["lessons"])
    assert periods == ["mon-1", "mon-3"]


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
