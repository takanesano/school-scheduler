"""Tests for the validator and solver — the correctness core."""
import pytest

from app.scheduler import (Dataset, Lesson, Room, Timeslot,
                           check_input_problems, coverage_report,
                           optimize_teacher_days, schedule_objective, solve,
                           student_day_stats, teacher_day_stats, validate)


# Each nickname maps to one concrete date of a summer term (7/27 = Mon).
DATE_OF = {"Mon": "2026-07-27", "Tue": "2026-07-28", "Wed": "2026-07-29",
           "Thu": "2026-07-30", "Fri": "2026-07-31", "Sat": "2026-08-01"}


def make_data(n_slots_per_day=3, days=("Mon", "Tue")) -> Dataset:
    """A small fully-connected world: everyone available everywhere.

    ``days`` are weekday nicknames resolved through DATE_OF, so slot ids
    stay readable ("mon-1") while timeslots carry real dates.
    """
    d = Dataset()
    d.students = {"s1": "Aoi", "s2": "Ren"}
    d.teachers = {"t1": "Tanaka", "t2": "Suzuki"}
    d.subjects = {"math": "Math", "eng": "English"}
    d.rooms = {"r1": Room("r1", "Room 1", 1)}
    for day in days:
        for p in range(1, n_slots_per_day + 1):
            sid = f"{day.lower()}-{p}"
            d.timeslots[sid] = Timeslot(sid, DATE_OF[day], p)
    d.teacher_subjects = {("t1", "math"), ("t1", "eng"),
                          ("t2", "math"), ("t2", "eng")}
    for t in d.teachers:
        for s in d.timeslots:
            d.teacher_availability.add((t, s))
    for st in d.students:
        for s in d.timeslots:
            d.student_availability.add((st, s))
    return d


def codes(violations):
    return sorted(v.code for v in violations)


# ------------------------------------------------------------------ validate

def test_empty_schedule_is_valid():
    assert validate(make_data(), []) == []


def test_valid_lesson_passes():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1)]
    assert validate(d, lessons) == []


def test_unknown_references_reported():
    d = make_data()
    lessons = [Lesson("ghost", "nope", "t9", "rX", "never", id=1)]
    vs = validate(d, lessons)
    assert codes(vs) == ["unknown_reference"]
    assert "ghost" in vs[0].message


def test_teacher_cannot_teach_subject():
    d = make_data()
    d.teacher_subjects.discard(("t1", "math"))
    vs = validate(d, [Lesson("s1", "math", "t1", "r1", "mon-1", id=1)])
    assert codes(vs) == ["teacher_cannot_teach"]


def test_teacher_unavailable():
    d = make_data()
    d.teacher_availability.discard(("t1", "mon-1"))
    vs = validate(d, [Lesson("s1", "math", "t1", "r1", "mon-1", id=1)])
    assert codes(vs) == ["teacher_unavailable"]


def test_student_unavailable():
    d = make_data()
    d.student_availability.discard(("s1", "mon-1"))
    vs = validate(d, [Lesson("s1", "math", "t1", "r1", "mon-1", id=1)])
    assert codes(vs) == ["student_unavailable"]


def test_teacher_can_pair_two_students_even_different_subjects():
    d = make_data()
    d.rooms["r2"] = Room("r2", "Room 2", 1)
    vs = validate(d, [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
                      Lesson("s2", "eng", "t1", "r2", "mon-1", id=2)])
    assert vs == []


def test_teacher_over_capacity_with_three_students():
    d = make_data()
    d.students["s3"] = "Yui"
    d.student_availability |= {("s3", s) for s in d.timeslots}
    d.rooms["r1"] = Room("r1", "Room 1", 3)
    vs = validate(d, [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
                      Lesson("s2", "eng", "t1", "r1", "mon-1", id=2),
                      Lesson("s3", "math", "t1", "r1", "mon-1", id=3)])
    assert codes(vs) == ["teacher_over_capacity"]
    assert set(vs[0].lesson_ids) == {1, 2, 3}


def test_teacher_capacity_parameter():
    d = make_data()
    d.rooms["r1"] = Room("r1", "Room 1", 2)
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s2", "eng", "t1", "r1", "mon-1", id=2)]
    assert validate(d, lessons) == []                       # default 2
    vs = validate(d, lessons, teacher_capacity=1)
    assert codes(vs) == ["teacher_over_capacity"]


def test_student_double_booked():
    d = make_data()
    d.rooms["r2"] = Room("r2", "Room 2", 1)
    vs = validate(d, [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
                      Lesson("s1", "eng", "t2", "r2", "mon-1", id=2)])
    assert codes(vs) == ["student_double_booked"]


def test_room_over_capacity():
    d = make_data()
    vs = validate(d, [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
                      Lesson("s2", "eng", "t2", "r1", "mon-1", id=2)])
    assert codes(vs) == ["room_over_capacity"]


def test_room_capacity_two_allows_two_lessons():
    d = make_data()
    d.rooms["r1"] = Room("r1", "Room 1", 2)
    vs = validate(d, [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
                      Lesson("s2", "eng", "t2", "r1", "mon-1", id=2)])
    assert vs == []


def test_student_two_consecutive_lessons_per_day_ok():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s1", "eng", "t1", "r1", "mon-2", id=2)]   # P1+P2
    assert validate(d, lessons) == []


def test_student_two_nonconsecutive_lessons_rejected():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s1", "eng", "t1", "r1", "mon-3", id=2)]   # P1+P3
    vs = validate(d, lessons)
    assert codes(vs) == ["student_day_gap"]
    assert set(vs[0].lesson_ids) == {1, 2}
    assert "consecutive" in vs[0].message


def test_student_three_lessons_per_day_rejected():
    d = make_data()
    d.subjects["sci"] = "Science"
    d.teacher_subjects |= {("t1", "sci"), ("t2", "sci")}
    d.rooms["r1"] = Room("r1", "Room 1", 2)
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s1", "eng", "t1", "r1", "mon-2", id=2),
               Lesson("s1", "sci", "t2", "r1", "mon-3", id=3)]
    vs = validate(d, lessons)
    assert codes(vs) == ["student_day_overload"]
    assert "max 2 per day" in vs[0].message


def test_student_day_cap_configurable():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s1", "eng", "t1", "r1", "mon-2", id=2)]
    assert validate(d, lessons) == []                       # cap 2 default
    vs = validate(d, lessons, student_day_cap=1)
    assert codes(vs) == ["student_day_overload"]
    assert "max 1 per day" in vs[0].message


def test_consecutiveness_can_be_disabled():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s1", "eng", "t1", "r1", "mon-3", id=2)]
    assert codes(validate(d, lessons)) == ["student_day_gap"]
    assert validate(d, lessons, require_consecutive=False) == []


def test_three_lessons_contiguity_with_higher_cap():
    def world(n_periods):
        d = make_data(n_slots_per_day=n_periods, days=("Mon",))
        d.subjects["sci"] = "Science"
        d.teacher_subjects |= {("t1", "sci"), ("t2", "sci")}
        d.rooms["r1"] = Room("r1", "Room 1", 3)
        return d

    triple = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
              Lesson("s1", "eng", "t1", "r1", "mon-2", id=2),
              Lesson("s1", "sci", "t2", "r1", "mon-3", id=3)]
    assert validate(world(3), triple, student_day_cap=3) == []   # P1-P2-P3

    gapped = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
              Lesson("s1", "eng", "t1", "r1", "mon-2", id=2),
              Lesson("s1", "sci", "t2", "r1", "mon-4", id=3)]    # P4: gap
    vs = validate(world(4), gapped, student_day_cap=3)
    assert codes(vs) == ["student_day_gap"]
    assert "P1, P2, P4" in vs[0].message


def test_objective_caps_reported_as_violations():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s2", "eng", "t1", "r1", "tue-1", id=2)]
    # t1 has 2 lessons, t2 none -> slot spread 2
    assert validate(d, lessons) == []
    vs = validate(d, lessons,
                  objective_caps={"teacher_slot_spread": 1})
    assert codes(vs) == ["objective_cap_exceeded"]
    assert "is 2 but must be at most 1" in vs[0].message
    assert vs[0].lesson_ids == ()
    assert validate(d, lessons,
                    objective_caps={"teacher_slot_spread": 2}) == []


def test_solve_with_day_cap_three_stays_contiguous():
    d = make_data(n_slots_per_day=4, days=("Mon",))
    d.subjects["sci"] = "Science"
    d.teacher_subjects |= {("t1", "sci"), ("t2", "sci")}
    d.rooms["r1"] = Room("r1", "Room 1", 2)
    d.student_needs = {("s1", "math"): 1, ("s1", "eng"): 1,
                       ("s1", "sci"): 1}
    assert not solve(d).complete                     # default cap 2
    r = solve(d, student_day_cap=3)
    assert r.complete
    assert validate(d, r.lessons, student_day_cap=3) == []
    periods = sorted(d.timeslots[l.timeslot_id].period for l in r.lessons)
    assert periods[-1] - periods[0] == 2             # contiguous triple


def test_same_subject_twice_in_a_day_now_allowed_if_consecutive():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s1", "math", "t1", "r1", "mon-2", id=2)]
    assert validate(d, lessons) == []


def test_multiple_violations_all_reported():
    d = make_data()
    d.teacher_subjects.discard(("t1", "math"))
    d.teacher_availability.discard(("t1", "mon-1"))
    d.student_availability.discard(("s1", "mon-1"))
    vs = validate(d, [Lesson("s1", "math", "t1", "r1", "mon-1", id=1)])
    assert codes(vs) == ["student_unavailable", "teacher_cannot_teach",
                         "teacher_unavailable"]


# ------------------------------------------------------------------ coverage

def test_coverage_unmet_and_exceeded_and_orphan():
    d = make_data()
    d.student_needs = {("s1", "math"): 2, ("s2", "eng"): 1}
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),   # 1 of 2
               Lesson("s2", "eng", "t2", "r1", "tue-1", id=2),
               Lesson("s2", "eng", "t2", "r1", "tue-2", id=3),    # 2 of 1
               Lesson("s2", "math", "t1", "r1", "mon-2", id=4)]   # no need
    got = codes(coverage_report(d, lessons))
    assert got == ["lesson_without_need", "need_exceeded", "need_unmet"]


def test_coverage_exact_match_is_clean():
    d = make_data()
    d.student_needs = {("s1", "math"): 1}
    assert coverage_report(
        d, [Lesson("s1", "math", "t1", "r1", "mon-1", id=1)]) == []


# --------------------------------------------------------------------- solve

def assert_solution_valid(d, result):
    assert validate(d, result.lessons) == []
    assert coverage_report(d, result.lessons) == []


def test_solve_simple_complete():
    d = make_data()
    d.student_needs = {("s1", "math"): 2, ("s1", "eng"): 1,
                       ("s2", "math"): 1, ("s2", "eng"): 2}
    r = solve(d)
    assert r.complete
    assert len(r.lessons) == 6
    assert_solution_valid(d, r)


def test_solve_deterministic():
    d = make_data()
    d.student_needs = {("s1", "math"): 2, ("s2", "eng"): 2}
    assert solve(d).lessons == solve(d).lessons


def test_solve_respects_room_capacity_bottleneck():
    # 1 room capacity 1, 2 slots total → at most 2 lessons can ever fit.
    d = make_data(n_slots_per_day=1, days=("Mon", "Tue"))
    d.student_needs = {("s1", "math"): 1, ("s2", "math"): 1,
                       ("s1", "eng"): 1}
    r = solve(d)
    assert not r.complete
    assert sum(n for (_, _, n) in r.unscheduled) == 1
    assert validate(d, r.lessons) == []


def test_solve_needs_backtracking():
    """Greedy assignment fails here; only backtracking finds the answer.

    One teacher, subjects math+eng.  Student s1 available only mon-1;
    s2 available mon-1 and mon-2.  If s2 is (greedily) placed at mon-1
    first, s1 cannot be scheduled — the solver must backtrack.
    """
    d = Dataset()
    d.students = {"s1": "A", "s2": "B"}
    d.teachers = {"t1": "T"}
    d.subjects = {"math": "Math"}
    d.rooms = {"r1": Room("r1", "R", 1)}
    d.timeslots = {"mon-1": Timeslot("mon-1", "2026-07-27", 1),
                   "mon-2": Timeslot("mon-2", "2026-07-27", 2)}
    d.teacher_subjects = {("t1", "math")}
    d.teacher_availability = {("t1", "mon-1"), ("t1", "mon-2")}
    d.student_availability = {("s1", "mon-1"),
                              ("s2", "mon-1"), ("s2", "mon-2")}
    d.student_needs = {("s1", "math"): 1, ("s2", "math"): 1}
    r = solve(d)
    assert r.complete
    assert_solution_valid(d, r)
    placed = {(l.student_id, l.timeslot_id) for l in r.lessons}
    assert ("s1", "mon-1") in placed
    assert ("s2", "mon-2") in placed


def test_solve_prefers_one_lesson_per_day():
    """Two sessions, two days available: the solver uses both days rather
    than stacking a two-lesson day."""
    d = make_data(n_slots_per_day=3, days=("Mon", "Tue"))
    d.student_needs = {("s1", "math"): 2}
    r = solve(d)
    assert r.complete
    dates = {d.timeslots[l.timeslot_id].date for l in r.lessons}
    assert dates == {DATE_OF["Mon"], DATE_OF["Tue"]}


def test_solve_worst_case_two_consecutive_on_one_day():
    """Only one day exists, so both sessions land there — in consecutive
    periods (the solver must not pick P1 and P3)."""
    d = make_data(n_slots_per_day=3, days=("Mon",))
    d.student_needs = {("s1", "math"): 2}
    r = solve(d)
    assert r.complete
    assert validate(d, r.lessons) == []
    periods = sorted(d.timeslots[l.timeslot_id].period for l in r.lessons)
    assert periods[1] - periods[0] == 1


def test_solve_three_sessions_never_stack_three_on_a_day():
    """Three sessions, two days: must split 2+1, never 3 on one day."""
    d = make_data(n_slots_per_day=3, days=("Mon", "Tue"))
    d.student_needs = {("s1", "math"): 2, ("s1", "eng"): 1}
    r = solve(d)
    assert r.complete
    assert_solution_valid(d, r)
    from collections import Counter
    per_day = Counter(d.timeslots[l.timeslot_id].date for l in r.lessons)
    assert max(per_day.values()) <= 2


def test_solve_infeasible_reports_unscheduled():
    d = make_data()
    d.teacher_subjects = set()  # nobody can teach anything
    d.student_needs = {("s1", "math"): 1}
    r = solve(d)
    assert not r.complete
    assert r.unscheduled == [("s1", "math", 1)]
    assert r.lessons == []


def test_solve_partial_schedule_is_still_valid():
    d = make_data(n_slots_per_day=1, days=("Mon",))
    d.student_needs = {("s1", "math"): 1, ("s1", "eng"): 1}  # only 1 slot
    r = solve(d)
    assert not r.complete
    assert len(r.lessons) == 1
    assert validate(d, r.lessons) == []


def test_solve_keeps_fixed_lessons():
    d = make_data()
    fixed = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1)]
    d.student_needs = {("s1", "math"): 2, ("s2", "eng"): 1}
    r = solve(d, fixed_lessons=fixed)
    assert r.complete
    assert fixed[0] in r.lessons
    assert_solution_valid(d, r)


def test_solve_fixed_lessons_count_toward_needs():
    d = make_data()
    fixed = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1)]
    d.student_needs = {("s1", "math"): 1}
    r = solve(d, fixed_lessons=fixed)
    assert r.complete
    assert len(r.lessons) == 1  # nothing new scheduled


def test_solve_room_capacity_shared():
    """Two students, one big room, one slot, two teachers → both fit."""
    d = make_data(n_slots_per_day=1, days=("Mon",))
    d.rooms = {"r1": Room("r1", "Open floor", 2)}
    d.student_needs = {("s1", "math"): 1, ("s2", "eng"): 1}
    r = solve(d)
    assert r.complete
    assert_solution_valid(d, r)
    assert {l.timeslot_id for l in r.lessons} == {"mon-1"}


def test_solve_unknown_student_in_needs_reported():
    d = make_data()
    d.student_needs = {("nobody", "math"): 1}
    r = solve(d)
    assert not r.complete
    assert ("nobody", "math", 1) in r.unscheduled


@pytest.mark.parametrize("seed_students", [3, 5])
def test_solve_medium_instance(seed_students):
    """A denser deterministic instance; solution must satisfy everything."""
    d = Dataset()
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]   # resolved via DATE_OF below
    for i in range(seed_students):
        d.students[f"s{i}"] = f"Student{i}"
    d.teachers = {"t1": "T1", "t2": "T2", "t3": "T3"}
    d.subjects = {"math": "Math", "eng": "Eng", "sci": "Sci"}
    d.rooms = {"r1": Room("r1", "R1", 2), "r2": Room("r2", "R2", 1)}
    for day in days:
        for p in (1, 2, 3):
            sid = f"{day.lower()}-{p}"
            d.timeslots[sid] = Timeslot(sid, DATE_OF[day], p)
    d.teacher_subjects = {("t1", "math"), ("t1", "sci"),
                          ("t2", "eng"), ("t2", "math"), ("t3", "sci"),
                          ("t3", "eng")}
    slots = list(d.timeslots)
    for ti, t in enumerate(d.teachers):
        for j, s in enumerate(slots):
            if (j + ti) % 3 != 0:  # each teacher misses a third of slots
                d.teacher_availability.add((t, s))
    for si, st in enumerate(d.students):
        for j, s in enumerate(slots):
            if (j + si) % 4 != 0:
                d.student_availability.add((st, s))
    subjects = list(d.subjects)
    for si, st in enumerate(d.students):
        d.student_needs[(st, subjects[si % 3])] = 2
        d.student_needs[(st, subjects[(si + 1) % 3])] = 1
    r = solve(d)
    assert r.complete, f"unscheduled: {r.unscheduled}"
    assert_solution_valid(d, r)


def test_solve_uses_teacher_pairing_when_needed():
    """1 teacher, 1 slot, 2 students needing different subjects: only
    solvable because the teacher can take both at once."""
    d = make_data(n_slots_per_day=1, days=("Mon",))
    d.teachers = {"t1": "Tanaka"}
    d.teacher_subjects = {("t1", "math"), ("t1", "eng")}
    d.teacher_availability = {("t1", "mon-1")}
    d.rooms = {"r1": Room("r1", "Room 1", 2)}
    d.student_needs = {("s1", "math"): 1, ("s2", "eng"): 1}
    r = solve(d)
    assert r.complete
    assert_solution_valid(d, r)
    assert {l.timeslot_id for l in r.lessons} == {"mon-1"}


def test_solve_respects_teacher_capacity_limit():
    """Three students, one teacher, one slot: only two fit."""
    d = make_data(n_slots_per_day=1, days=("Mon",))
    d.students["s3"] = "Yui"
    d.student_availability |= {("s3", "mon-1")}
    d.teachers = {"t1": "Tanaka"}
    d.teacher_subjects = {("t1", "math")}
    d.teacher_availability = {("t1", "mon-1")}
    d.rooms = {"r1": Room("r1", "Room 1", 5)}
    d.student_needs = {("s1", "math"): 1, ("s2", "math"): 1,
                       ("s3", "math"): 1}
    r = solve(d)
    assert not r.complete
    assert len(r.lessons) == 2
    assert validate(d, r.lessons) == []


# ------------------------------------------------- teacher-day optimization

def test_objective_counts_slots_days_and_spreads():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s2", "eng", "t1", "r1", "tue-1", id=2)]
    stats = teacher_day_stats(d, lessons)
    assert stats["t1"]["lessons"] == 2
    assert stats["t1"]["days"] == {DATE_OF["Mon"], DATE_OF["Tue"]}
    assert stats["t2"] == {"lessons": 0, "days": set()}
    # t1: 2 lessons/2 days, t2: 0/0 -> spreads 2, total days 2
    assert schedule_objective(d, lessons) == (0, 2, 2, 2)


def test_student_day_stats_reports_double_days():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s1", "eng", "t2", "r1", "mon-2", id=2),   # 2 on Mon
               Lesson("s1", "math", "t1", "r1", "tue-1", id=3),
               Lesson("s2", "eng", "t2", "r1", "tue-2", id=4)]
    stats = student_day_stats(d, lessons)
    assert stats["s1"] == {"lessons": 3, "days": 2,
                           "double_days": [DATE_OF["Mon"]]}
    assert stats["s2"] == {"lessons": 1, "days": 1, "double_days": []}


def test_student_day_stats_ignores_unknown_slots():
    d = make_data()
    stats = student_day_stats(
        d, [Lesson("s1", "math", "t1", "r1", "ghost", id=1)])
    assert stats["s1"] == {"lessons": 0, "days": 0, "double_days": []}


def test_objective_ignores_ineligible_teachers():
    d = make_data()
    d.teachers["t9"] = "Ghost"          # no subjects, no availability
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s2", "eng", "t2", "r1", "mon-1", id=2)]
    assert schedule_objective(d, lessons) == (0, 0, 2, 0)   # t9 not counted


def test_optimize_packs_teacher_into_fewer_days():
    """t1 teaches Mon P1 and Tue P1; the Tue lesson can move to Mon P2."""
    d = make_data()
    d.teachers = {"t1": "Tanaka"}
    d.teacher_subjects = {("t1", "math"), ("t1", "eng")}
    d.teacher_availability = {("t1", s) for s in d.timeslots}
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1"),
               Lesson("s2", "eng", "t1", "r1", "tue-1")]
    out = optimize_teacher_days(d, lessons)
    assert validate(d, out) == []
    assert schedule_objective(d, out) == (0, 0, 1, 0)
    assert sorted((l.student_id, l.subject_id) for l in out) == \
        [("s1", "math"), ("s2", "eng")]              # coverage untouched


def test_optimize_balances_days_across_teachers():
    """t1 works 2 days, t2 none; a whole-day handover evens it out
    without increasing total working days."""
    d = make_data()
    # both teachers can teach both subjects, available everywhere (make_data)
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1"),
               Lesson("s2", "eng", "t1", "r1", "tue-1")]
    # packing to (1, x) is impossible: s1 is only free Mon, s2 only Tue
    d.student_availability = {("s1", "mon-1"), ("s1", "mon-2"),
                              ("s2", "tue-1"), ("s2", "tue-2")}
    out = optimize_teacher_days(d, lessons)
    assert validate(d, out) == []
    assert schedule_objective(d, out) == (0, 0, 2, 0)   # one lesson+day each
    assert {l.teacher_id for l in out} == {"t1", "t2"}


def test_optimize_keeps_fixed_lessons_pinned():
    d = make_data()
    d.teachers = {"t1": "Tanaka"}
    d.teacher_subjects = {("t1", "math"), ("t1", "eng")}
    d.teacher_availability = {("t1", s) for s in d.timeslots}
    fixed = [Lesson("s1", "math", "t1", "r1", "tue-1", id=99)]
    movable = [Lesson("s2", "eng", "t1", "r1", "mon-1")]
    out = optimize_teacher_days(d, movable, fixed=fixed)
    assert fixed[0] in out                            # untouched
    # the movable lesson joins the fixed lesson's day instead
    moved = next(l for l in out if l.student_id == "s2")
    assert d.timeslots[moved.timeslot_id].date == DATE_OF["Tue"]
    assert schedule_objective(d, out) == (0, 0, 1, 0)


def test_optimize_rebalances_idle_teacher_even_at_day_cost():
    """t1 has 2 lessons on one day, t2 has none and is only available on a
    different day. Balancing lesson counts outranks day packing, so one
    lesson moves to t2 even though total working days rises from 1 to 2."""
    d = make_data()
    d.teacher_availability = (
        {("t1", s) for s in d.timeslots}
        | {("t2", "tue-1"), ("t2", "tue-2")})
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1"),
               Lesson("s2", "eng", "t1", "r1", "mon-2")]
    out = optimize_teacher_days(d, lessons)
    assert validate(d, out) == []
    counts = sorted((l.teacher_id for l in out))
    assert counts == ["t1", "t2"]                    # one lesson each
    assert schedule_objective(d, out) == (0, 0, 2, 0)


def test_optimize_balances_realistic_lopsided_schedule():
    """Both teachers fully capable and available; all six lessons start on
    t1. Balance must end within one lesson of each other."""
    d = make_data(n_slots_per_day=3, days=("Mon", "Tue"))
    d.students.update({"s3": "Yui", "s4": "Haru"})
    for st in ("s3", "s4"):
        d.student_availability |= {(st, s) for s in d.timeslots}
    d.rooms["r1"] = Room("r1", "Room 1", 4)
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1"),
               Lesson("s2", "math", "t1", "r1", "mon-1"),
               Lesson("s3", "eng", "t1", "r1", "mon-2"),
               Lesson("s4", "eng", "t1", "r1", "mon-2"),
               Lesson("s1", "eng", "t1", "r1", "tue-1"),
               Lesson("s2", "eng", "t1", "r1", "tue-2")]
    out = optimize_teacher_days(d, lessons)
    assert validate(d, out) == []
    from collections import Counter
    loads = Counter(l.teacher_id for l in out)
    assert max(loads.values()) - min(loads.values()) <= 1
    assert loads.total() == 6


def test_schedule_objective_custom_order_permutes_tuple():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
               Lesson("s1", "eng", "t1", "r1", "mon-2", id=2)]
    default = schedule_objective(d, lessons)
    reordered = schedule_objective(
        d, lessons, ["teacher_working_day", "teacher_day_spread",
                     "student_double_day", "teacher_slot_spread"])
    assert reordered == (default[2], default[3], default[0], default[1])


def test_optimizer_honors_objective_order():
    """Same instance as the idle-teacher rebalance test: with the default
    order (balance first) a lesson moves to t2 at the cost of a second
    working day; with few-working-days prioritized, everything stays on
    t1's single day."""
    def build():
        d = make_data()
        d.teacher_availability = (
            {("t1", s) for s in d.timeslots}
            | {("t2", "tue-1"), ("t2", "tue-2")})
        return d, [Lesson("s1", "math", "t1", "r1", "mon-1"),
                   Lesson("s2", "eng", "t1", "r1", "mon-2")]

    d, lessons = build()
    balanced = optimize_teacher_days(d, list(lessons))
    assert {l.teacher_id for l in balanced} == {"t1", "t2"}

    d, lessons = build()
    days_first = optimize_teacher_days(
        d, list(lessons),
        objective_order=["student_double_day", "teacher_working_day",
                         "teacher_slot_spread", "teacher_day_spread"])
    assert {l.teacher_id for l in days_first} == {"t1"}   # 1 day kept


def test_optimize_never_touches_invalid_schedule():
    d = make_data()
    bad = [Lesson("s1", "math", "t1", "r1", "mon-1", id=1),
           Lesson("s1", "eng", "t2", "r1", "mon-2", id=2),
           Lesson("s1", "math", "t2", "r1", "ghost", id=3)]
    assert optimize_teacher_days(d, bad) == bad


def test_optimize_is_deterministic():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1"),
               Lesson("s2", "eng", "t2", "r1", "tue-1"),
               Lesson("s2", "math", "t1", "r1", "wed-1")
               if "wed-1" in d.timeslots else
               Lesson("s2", "math", "t1", "r1", "tue-2")]
    a = optimize_teacher_days(d, list(lessons))
    b = optimize_teacher_days(d, list(lessons))
    assert a == b


# --------------------------------------------------------- input diagnostics

def test_check_input_no_teacher_for_subject():
    d = make_data()
    d.teacher_subjects = {("t1", "eng"), ("t2", "eng")}
    d.student_needs = {("s1", "math"): 1}
    problems = check_input_problems(d)
    assert len(problems) == 1
    assert "No teacher can teach Math" in problems[0]


def test_check_input_not_enough_overlapping_slots():
    d = make_data(n_slots_per_day=2, days=("Mon",))
    d.student_availability = {("s1", "mon-1"), ("s2", "mon-1"), ("s2", "mon-2")}
    d.student_needs = {("s1", "math"): 2}
    problems = check_input_problems(d)
    assert len(problems) == 1
    assert "only 1 timeslot(s)" in problems[0]


def test_check_input_clean():
    d = make_data()
    d.student_needs = {("s1", "math"): 2}
    assert check_input_problems(d) == []
