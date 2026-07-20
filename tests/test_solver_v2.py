"""Tests for solver v2 (CP-SAT). Skipped gracefully if ortools is absent
except for the parts that must work without it."""
from pathlib import Path

import pytest

from app import db
from app.load_sample import load_directory
from app.main import load_dataset
from app.scheduler import Lesson, Room, coverage_report, validate
from app.solver_v2 import (ObjectiveWeights, SolverConfig, objective_terms,
                           resolve_minimal_disruption, solve_v2,
                           weighted_cost)
from tests.test_scheduler import make_data

ortools = pytest.importorskip("ortools", reason="ortools not installed")

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"

# small reproducible CP budget so the suite stays fast; solve_v2's cost
# gate guarantees quality never drops below the v1 pipeline
FAST = SolverConfig(deterministic_time=3.0)


def sample_dataset(tmp_path):
    db_path = tmp_path / "s.db"
    load_directory(SAMPLE_DIR, db_path)
    conn = db.connect(db_path)
    try:
        return load_dataset(conn)
    finally:
        conn.close()


def assert_clean(d, r, cap=2):
    assert r.complete
    assert validate(d, r.lessons, cap) == []
    assert coverage_report(d, r.lessons) == []


# --------------------------------------------------------------- unit pieces

def test_lexicographic_weights_dominate_in_order():
    w = ObjectiveWeights.lexicographic()
    assert w.student_double_day > w.teacher_slot_spread \
        > w.teacher_working_day > w.teacher_day_spread > 0


def test_lexicographic_weights_follow_custom_order():
    w = ObjectiveWeights.lexicographic(
        ["teacher_working_day", "student_double_day",
         "teacher_day_spread", "teacher_slot_spread"])
    assert w.teacher_working_day > w.student_double_day \
        > w.teacher_day_spread > w.teacher_slot_spread > 0
    with pytest.raises(ValueError):
        ObjectiveWeights.lexicographic(["student_double_day"])


def test_objective_terms_and_weighted_cost():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1"),
               Lesson("s1", "eng", "t1", "r1", "mon-2"),   # 2 on Mon
               Lesson("s2", "eng", "t2", "r1", "tue-1")]
    terms = objective_terms(d, lessons)
    assert terms == {"student_double_day": 1, "teacher_slot_spread": 1,
                     "teacher_working_day": 2, "teacher_day_spread": 0,
                     "changed_lesson": 0}
    cfg = SolverConfig(weights=ObjectiveWeights(
        student_double_day=10, teacher_slot_spread=5,
        teacher_working_day=1))
    assert weighted_cost(d, lessons, cfg) == 10 + 5 + 2


def test_changed_lesson_term_counts_reference_diffs():
    d = make_data()
    a = Lesson("s1", "math", "t1", "r1", "mon-1")
    b = Lesson("s2", "eng", "t2", "r1", "tue-1")
    moved = Lesson("s2", "eng", "t2", "r1", "tue-2")
    terms = objective_terms(d, [a, moved], reference=[a, b])
    assert terms["changed_lesson"] == 1


# ------------------------------------------------------------------- CP-SAT

def test_cpsat_solves_and_wins_on_small_instance():
    d = make_data()
    d.student_needs = {("s1", "math"): 2, ("s1", "eng"): 1,
                       ("s2", "math"): 1, ("s2", "eng"): 2}
    r = solve_v2(d)
    assert r.backend == "cpsat"
    assert_clean(d, r)
    cfg = SolverConfig()
    from app.solver_v2 import _v1_pipeline
    v1 = _v1_pipeline(d, cfg, None)
    assert weighted_cost(d, r.lessons, cfg) <= \
        weighted_cost(d, v1.lessons, cfg)


def test_cpsat_on_sample_term_beats_or_matches_v1(tmp_path):
    d = sample_dataset(tmp_path)
    cfg = FAST
    r = solve_v2(d, config=cfg)
    assert r.backend == "cpsat"
    assert_clean(d, r)
    assert len(r.lessons) == 30
    from app.solver_v2 import _v1_pipeline
    v1 = _v1_pipeline(d, cfg, None)
    assert weighted_cost(d, r.lessons, cfg) <= \
        weighted_cost(d, v1.lessons, cfg)
    # the strongest student guarantee holds on the real data
    assert objective_terms(d, r.lessons)["student_double_day"] == 0


def test_cpsat_is_deterministic(tmp_path):
    d = sample_dataset(tmp_path)
    assert solve_v2(d, config=FAST).lessons == \
        solve_v2(d, config=FAST).lessons


def test_cpsat_respects_pinned_lessons():
    d = make_data()
    pin = Lesson("s1", "math", "t1", "r1", "tue-3", id=7)
    d.student_needs = {("s1", "math"): 2, ("s2", "eng"): 1}
    r = solve_v2(d, fixed_lessons=[pin])
    assert r.backend == "cpsat"
    assert pin in r.lessons
    assert_clean(d, r)


def test_cpsat_honors_consecutiveness_and_day_cap():
    """One day only: the two sessions must land in adjacent periods."""
    d = make_data(n_slots_per_day=3, days=("Mon",))
    d.student_needs = {("s1", "math"): 2}
    r = solve_v2(d)
    assert r.backend == "cpsat"
    assert_clean(d, r)
    periods = sorted(d.timeslots[l.timeslot_id].period for l in r.lessons)
    assert periods[1] - periods[0] == 1


def test_cpsat_prefers_one_lesson_per_day():
    d = make_data(n_slots_per_day=3, days=("Mon", "Tue"))
    d.student_needs = {("s1", "math"): 2}
    r = solve_v2(d)
    assert objective_terms(d, r.lessons)["student_double_day"] == 0


def test_cpsat_respects_capacity_config():
    d = make_data()
    d.rooms["r1"] = Room("r1", "Room 1", 2)
    d.student_needs = {("s1", "math"): 1, ("s2", "eng"): 1}
    cfg = SolverConfig(teacher_capacity=1)
    r = solve_v2(d, config=cfg)
    assert r.complete
    assert validate(d, r.lessons, teacher_capacity=1) == []


def test_infeasible_falls_back_to_v1_partial():
    d = make_data(n_slots_per_day=1, days=("Mon",))
    d.student_needs = {("s1", "math"): 1, ("s1", "eng"): 1}  # 1 slot only
    r = solve_v2(d)
    assert r.backend == "v1"
    assert not r.complete
    assert len(r.lessons) == 1
    assert validate(d, r.lessons) == []


def test_zero_time_budget_still_returns_valid_schedule():
    d = make_data()
    d.student_needs = {("s1", "math"): 2, ("s2", "eng"): 1}
    r = solve_v2(d, config=SolverConfig(deterministic_time=1e-9,
                                        time_limit_seconds=1e-9))
    assert r.complete                       # v1 fallback covers it
    assert validate(d, r.lessons) == []


def test_weights_change_the_answer():
    """Same instance, two weightings, two different optima.

    t1 available everywhere, t2 only Tuesday. Balance-first (v1 order)
    must use both teachers; a days-dominated weighting must give
    everything to t1 to keep one working day.
    """
    d = make_data()
    d.teacher_availability = (
        {("t1", s) for s in d.timeslots}
        | {("t2", "tue-1"), ("t2", "tue-2")})
    d.student_needs = {("s1", "math"): 1, ("s2", "eng"): 1}

    balanced = solve_v2(d)                  # lexicographic preset
    assert {l.teacher_id for l in balanced.lessons} == {"t1", "t2"}

    days_first = SolverConfig(weights=ObjectiveWeights(
        student_double_day=1_000_000, teacher_working_day=100))
    packed = solve_v2(d, config=days_first)
    assert packed.backend == "cpsat"
    # one teacher handles everything on a single working day (whether
    # that is t1 on any day or t2 on Tuesday is equally optimal)
    assert len({l.teacher_id for l in packed.lessons}) == 1
    assert objective_terms(d, packed.lessons)["teacher_working_day"] == 1


# ------------------------------------------- minimal-disruption rescheduling

def test_resolve_keeps_schedule_when_still_valid(tmp_path):
    d = sample_dataset(tmp_path)
    current = solve_v2(d, config=FAST).lessons
    r = resolve_minimal_disruption(d, current, config=FAST)
    assert r.backend == "cpsat"
    assert sorted(map(str, r.lessons)) == sorted(map(str, current))


def test_resolve_repairs_with_exactly_one_change():
    """Handcrafted case where the one-change repair is unambiguous:
    t1 calls in sick for s1's slot; t2 teaches the same subject and is
    free there, so swapping the teacher fixes everything."""
    d = make_data()
    d.student_needs = {("s1", "math"): 1, ("s2", "eng"): 1}
    current = [Lesson("s1", "math", "t1", "r1", "mon-1"),
               Lesson("s2", "eng", "t2", "r1", "tue-1")]
    assert validate(d, current) == []
    d.teacher_availability.discard(("t1", "mon-1"))
    r = resolve_minimal_disruption(d, current)
    assert r.backend == "cpsat"
    assert validate(d, r.lessons) == []
    assert objective_terms(d, r.lessons,
                           reference=current)["changed_lesson"] == 1
    # the untouched lesson survived verbatim
    assert current[1] in r.lessons


def test_resolve_repairs_sample_term_with_few_changes(tmp_path):
    d = sample_dataset(tmp_path)
    current = solve_v2(d, config=FAST).lessons
    # a teacher calls in sick for one of their timeslots
    hit = current[0]
    d.teacher_availability.discard((hit.teacher_id, hit.timeslot_id))
    r = resolve_minimal_disruption(d, current, config=FAST)
    assert r.backend == "cpsat"
    assert_clean(d, r)
    changed = objective_terms(d, r.lessons,
                              reference=current)["changed_lesson"]
    assert 1 <= changed <= 2      # the broken lesson, plus at most one more
