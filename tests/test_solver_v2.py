"""Skeleton tests for solver v2 — pins the public contract while the
implementation is pending (see docs/solver-v2-plan.md)."""
import pytest

from app.scheduler import coverage_report, validate
from app.solver_v2 import (ObjectiveWeights, SolverConfig,
                           resolve_minimal_disruption, solve_v2,
                           weighted_cost)
from tests.test_scheduler import make_data


def test_lexicographic_weights_dominate_in_order():
    w = ObjectiveWeights.lexicographic()
    assert w.student_double_day > w.teacher_slot_spread \
        > w.teacher_working_day > w.teacher_day_spread > 0


def test_solve_v2_falls_back_to_v1_pipeline():
    d = make_data()
    d.student_needs = {("s1", "math"): 2, ("s2", "eng"): 1}
    r = solve_v2(d)
    assert r.complete
    assert validate(d, r.lessons) == []
    assert coverage_report(d, r.lessons) == []


def test_solve_v2_is_deterministic():
    d = make_data()
    d.student_needs = {("s1", "math"): 2, ("s2", "eng"): 2}
    assert solve_v2(d).lessons == solve_v2(d).lessons


def test_solve_v2_respects_config_capacity():
    d = make_data()
    cfg = SolverConfig(teacher_capacity=1)
    d.teachers = {"t1": "Tanaka"}
    d.teacher_subjects = {("t1", "math"), ("t1", "eng")}
    d.teacher_availability = {("t1", s) for s in d.timeslots}
    d.student_needs = {("s1", "math"): 1, ("s2", "eng"): 1}
    r = solve_v2(d, config=cfg)
    assert r.complete
    assert validate(d, r.lessons, teacher_capacity=1) == []


def test_unimplemented_phases_raise_cleanly():
    d = make_data()
    with pytest.raises(NotImplementedError):
        weighted_cost(d, [], SolverConfig())
    with pytest.raises(NotImplementedError):
        resolve_minimal_disruption(d, [])
