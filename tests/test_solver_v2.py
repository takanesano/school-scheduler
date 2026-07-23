"""Tests for solver v2 (CP-SAT). Skipped gracefully if ortools is absent
except for the parts that must work without it."""
import pytest

from app.scheduler import (Dataset, Lesson, Room, Timeslot, coverage_report,
                           validate)
from app.solver_v2 import (ObjectiveWeights, SolverConfig, objective_terms,
                           resolve_minimal_disruption, solve_v2,
                           weighted_cost)
from tests.test_scheduler import make_data

ortools = pytest.importorskip("ortools", reason="ortools not installed")

# small reproducible CP budget so the suite stays fast; solve_v2's cost
# gate guarantees quality never drops below the v1 pipeline
FAST = SolverConfig(deterministic_time=3.0)


def medium_dataset() -> Dataset:
    """A two-week term the size of the ORIGINAL sample data (the shipped
    sample is now a 1300-lesson stress test — far too big for these
    focused solver-behavior tests)."""
    d = Dataset()
    d.students = {f"s{i}": f"Student{i}" for i in range(1, 6)}
    d.teachers = {"t1": "T1", "t2": "T2", "t3": "T3"}
    d.subjects = {"math": "Math", "eng": "English", "sci": "Science"}
    d.rooms = {"hall": Room("hall", "Hall", 4)}
    import datetime as dt
    day = dt.date(2026, 8, 3)                      # a Monday
    while day <= dt.date(2026, 8, 14):
        if day.weekday() < 5:                      # Mon-Fri x 2 weeks
            for p in (1, 2, 3):
                sid = f"{day:%m%d}-{p}"
                d.timeslots[sid] = Timeslot(sid, day.isoformat(), p)
        day += dt.timedelta(days=1)
    d.teacher_subjects = {("t1", "math"), ("t1", "sci"),
                          ("t2", "eng"), ("t2", "math"),
                          ("t3", "sci"), ("t3", "eng")}
    for t in d.teachers:
        for s in d.timeslots:
            d.teacher_availability.add((t, s))
    for st in d.students:
        for s in d.timeslots:
            d.student_availability.add((st, s))
    subj = ["math", "eng", "sci"]
    for i, st in enumerate(sorted(d.students)):
        d.student_needs[(st, subj[i % 3])] = 3
        d.student_needs[(st, subj[(i + 1) % 3])] = 3
    return d


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
        ["teacher_working_day", "student_double_day", "student_day_gap",
         "teacher_single_day", "teacher_day_spread",
         "teacher_slot_spread"])
    assert w.teacher_working_day > w.student_double_day \
        > w.student_day_gap > w.teacher_single_day \
        > w.teacher_day_spread > w.teacher_slot_spread > 0
    with pytest.raises(ValueError):
        ObjectiveWeights.lexicographic(["student_double_day"])


def test_objective_terms_and_weighted_cost():
    d = make_data()
    lessons = [Lesson("s1", "math", "t1", "r1", "mon-1"),
               Lesson("s1", "eng", "t1", "r1", "mon-2"),   # 2 on Mon
               Lesson("s2", "eng", "t2", "r1", "tue-1")]
    terms = objective_terms(d, lessons)
    assert terms == {"student_double_day": 1, "student_day_gap": 0,
                     "teacher_slot_spread": 1, "teacher_working_day": 2,
                     "teacher_single_day": 1, "teacher_day_spread": 0,
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


def test_cpsat_on_medium_term_beats_or_matches_v1():
    d = medium_dataset()
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


def test_cpsat_is_deterministic():
    d = medium_dataset()
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
    assert r.v2_outcome == "infeasible"
    assert not r.complete
    assert len(r.lessons) == 1
    assert validate(d, r.lessons) == []


def test_v2_outcome_reported():
    """solve_v2 tells the caller what the exact optimizer actually did:
    proved-optimal on a trivial instance, and 'infeasible' when an
    always-active bound admits no schedule at all."""
    d = make_data()
    d.student_needs = {("s1", "math"): 1}
    r = solve_v2(d)
    assert r.backend == "cpsat"
    assert r.v2_outcome == "optimal"

    capped = solve_v2(d, config=SolverConfig(
        objective_caps={"teacher_working_day": 0}))
    assert capped.backend == "v1"
    assert capped.v2_outcome == "infeasible"

    # plain v1 solves never set an exact-optimizer outcome
    from app.scheduler import solve
    assert solve(d).v2_outcome is None


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


def test_cpsat_enforces_promoted_objective_cap():
    """Days-first weights alone would give everything to one teacher
    (spread 2); promoting the balance objective to a hard cap of 0 forces
    an even split regardless of the weights."""
    d = make_data()
    d.student_needs = {("s1", "math"): 1, ("s2", "eng"): 1}
    days_first = ObjectiveWeights.lexicographic(
        ["student_double_day", "student_day_gap", "teacher_working_day",
         "teacher_single_day", "teacher_slot_spread",
         "teacher_day_spread"])
    free = solve_v2(d, config=SolverConfig(weights=days_first))
    assert len({l.teacher_id for l in free.lessons}) == 1

    capped = SolverConfig(weights=days_first,
                          objective_caps={"teacher_slot_spread": 0})
    r = solve_v2(d, config=capped)
    assert r.backend == "cpsat"
    assert len({l.teacher_id for l in r.lessons}) == 2
    assert objective_terms(d, r.lessons)["teacher_slot_spread"] == 0


def test_always_active_is_stronger_than_any_priority_order():
    """The user's scenario: 'one class per student per day' set as ALWAYS
    ACTIVE — not merely first priority. Even with that condition dragged
    to the very BOTTOM of the priorities (where the weight order would
    happily accept a two-lesson day to save a teacher working day),
    marking it always-active still forbids the two-lesson day."""
    d = make_data()
    d.teachers = {"t1": "Tanaka"}
    d.teacher_subjects = {("t1", "math"), ("t1", "eng")}
    d.teacher_availability = {("t1", s) for s in d.timeslots}
    d.student_needs = {("s1", "math"): 1, ("s1", "eng"): 1}
    demoted = ObjectiveWeights.lexicographic(
        ["teacher_working_day", "teacher_slot_spread",
         "teacher_single_day", "teacher_day_spread", "student_day_gap",
         "student_double_day"])

    free = solve_v2(d, config=SolverConfig(weights=demoted))
    assert objective_terms(d, free.lessons)["student_double_day"] == 1
    assert objective_terms(d, free.lessons)["teacher_working_day"] == 1

    capped = solve_v2(d, config=SolverConfig(
        weights=demoted, objective_caps={"student_double_day": 0}))
    assert capped.backend == "cpsat"
    assert capped.complete
    assert objective_terms(d, capped.lessons)["student_double_day"] == 0
    # the price (a second working day) was paid, as it must be
    assert objective_terms(d, capped.lessons)["teacher_working_day"] == 2


def test_cpsat_minimizes_teacher_single_lesson_days():
    """s1 can only attend Mon P1; s2 is free everywhere. Placing s2 on a
    different day would leave the sole teacher with two single-lesson
    days — weighting teacher_single_day pulls s2 onto Monday instead."""
    d = make_data()
    d.teachers = {"t1": "Tanaka"}
    d.teacher_subjects = {("t1", "math"), ("t1", "eng")}
    d.teacher_availability = {("t1", s) for s in d.timeslots}
    d.student_availability = (
        {("s1", "mon-1")} | {("s2", s) for s in d.timeslots})
    d.student_needs = {("s1", "math"): 1, ("s2", "eng"): 1}
    cfg = SolverConfig(weights=ObjectiveWeights(teacher_single_day=100))
    r = solve_v2(d, config=cfg)
    assert r.backend == "cpsat"
    assert r.complete
    assert objective_terms(d, r.lessons)["teacher_single_day"] == 0
    days = {d.timeslots[l.timeslot_id].date for l in r.lessons}
    assert days == {"2026-07-27"}          # both lessons share Monday


def test_cpsat_honors_single_day_threshold():
    """single_day_max=2 counts two-lesson days as 'too few'. s1/s2 are
    Monday-only, s4 is Tuesday-only, s3 can do either. At the default
    threshold the optimum splits 2+2 (no single-lesson day); with
    threshold 2 that costs 2, so the optimum packs s3 onto Monday
    (3+1, cost 1)."""
    d = make_data()
    d.students = {f"s{i}": f"S{i}" for i in range(1, 5)}
    d.teachers = {"t1": "Tanaka"}
    d.teacher_subjects = {("t1", "math")}
    d.teacher_availability = {("t1", s) for s in d.timeslots}
    d.student_availability = {
        ("s1", "mon-1"), ("s2", "mon-2"),
        ("s3", "mon-3"), ("s3", "tue-1"), ("s4", "tue-2")}
    d.student_needs = {(st, "math"): 1 for st in d.students}
    w = ObjectiveWeights(teacher_single_day=100)

    split = solve_v2(d, config=SolverConfig(weights=w))
    assert split.backend == "cpsat" and split.complete
    mon = [l for l in split.lessons
           if d.timeslots[l.timeslot_id].date == "2026-07-27"]
    assert len(mon) == 2                    # 2+2: zero single-lesson days
    assert objective_terms(d, split.lessons)["teacher_single_day"] == 0

    packed = solve_v2(d, config=SolverConfig(weights=w, single_day_max=2))
    assert packed.backend == "cpsat" and packed.complete
    mon = [l for l in packed.lessons
           if d.timeslots[l.timeslot_id].date == "2026-07-27"]
    assert len(mon) == 3                    # 3+1 beats 2+2 at threshold 2
    assert objective_terms(d, packed.lessons,
                           single_day_max=2)["teacher_single_day"] == 1


def test_cpsat_enforces_single_day_threshold_cap():
    """The promoted cap and the CP encoding must agree on the threshold.
    A 2+2 split satisfies a teacher_single_day<=0 cap at the default
    threshold, but with single_day_max=2 every worked day needs >=3
    lessons — the only such layout puts all four lessons on Monday
    (pairing s3 and s4 in the last period)."""
    d = make_data()
    d.students = {f"s{i}": f"S{i}" for i in range(1, 5)}
    d.teachers = {"t1": "Tanaka"}
    d.teacher_subjects = {("t1", "math")}
    d.teacher_availability = {("t1", s) for s in d.timeslots}
    d.rooms = {"r1": Room("r1", "Room 1", 2)}     # allow slot pairing
    d.student_availability = {
        ("s1", "mon-1"), ("s2", "mon-2"),
        ("s3", "mon-3"), ("s3", "tue-1"),
        ("s4", "mon-3"), ("s4", "tue-1")}
    d.student_needs = {(st, "math"): 1 for st in d.students}
    cfg = SolverConfig(weights=ObjectiveWeights.lexicographic(),
                       single_day_max=2,
                       objective_caps={"teacher_single_day": 0})
    r = solve_v2(d, config=cfg)
    assert r.backend == "cpsat" and r.complete
    assert validate(d, r.lessons, objective_caps=cfg.objective_caps,
                    single_day_max=2) == []
    days = {d.timeslots[l.timeslot_id].date for l in r.lessons}
    assert days == {"2026-07-27"}                 # everything on Monday


def test_cpsat_respects_room_teacher_limit():
    """The reference pulls both lessons into mon-1 (dominating
    changed_lesson weight), but the room admits only one teacher per
    slot — the CP model must deviate instead of matching it."""
    d = make_data()
    d.rooms = {"r1": Room("r1", "Room 1", 2, teacher_capacity=1)}
    d.teacher_subjects = {("t1", "math"), ("t2", "eng")}
    d.student_needs = {("s1", "math"): 1, ("s2", "eng"): 1}
    reference = [Lesson("s1", "math", "t1", "r1", "mon-1"),
                 Lesson("s2", "eng", "t2", "r1", "mon-1")]
    cfg = SolverConfig(weights=ObjectiveWeights(
        changed_lesson=100_000_000.0))
    r = solve_v2(d, config=cfg, reference=reference)
    assert r.backend == "cpsat"
    assert r.complete
    assert validate(d, r.lessons) == []
    assert len({(l.room_id, l.timeslot_id) for l in r.lessons}) == 2


def test_cpsat_higher_day_cap_contiguous_triple():
    d = make_data(n_slots_per_day=4, days=("Mon",))
    d.subjects["sci"] = "Science"
    d.teacher_subjects |= {("t1", "sci"), ("t2", "sci")}
    d.rooms["r1"] = Room("r1", "Room 1", 2)
    d.student_needs = {("s1", "math"): 1, ("s1", "eng"): 1,
                       ("s1", "sci"): 1}
    cfg = SolverConfig(student_day_cap=3)
    r = solve_v2(d, config=cfg)
    assert r.complete
    assert validate(d, r.lessons, student_day_cap=3) == []
    periods = sorted(d.timeslots[l.timeslot_id].period for l in r.lessons)
    assert periods[-1] - periods[0] == 2


# ------------------------------------------- minimal-disruption rescheduling

def test_resolve_keeps_schedule_when_still_valid():
    d = medium_dataset()
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


def test_resolve_repairs_sample_term_with_few_changes():
    d = medium_dataset()
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
