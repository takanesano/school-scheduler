"""Solver v2 skeleton — tailored, weight-driven schedule optimization.

STATUS: skeleton only. Nothing here is wired into the API yet; the v1
pipeline (``solve`` + ``optimize_teacher_days``) remains the production
path. See docs/solver-v2-plan.md for the full design and phasing.

The core ideas this module will implement:

* one ``SolverConfig`` describing hard-constraint parameters and
  per-objective WEIGHTS (replacing v1's fixed lexicographic order);
* a single weighted cost function shared by every backend;
* a CP-SAT backend (optional ``ortools`` dependency) that optimizes the
  whole problem at once instead of feasibility-then-hill-climb;
* minimal-disruption rescheduling (penalize diffs against the current
  schedule) for mid-term changes.

Invariants (enforced by ``solve_v2``, never left to the backend):
* every result is re-checked with ``scheduler.validate`` — the validator
  stays the single source of truth; an invalid backend result is
  discarded in favor of the v1 answer;
* deterministic: same input + config → same schedule;
* pinned lessons are never moved;
* exactly ``sessions`` lessons per (student, subject).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .scheduler import (Dataset, Lesson, SolveResult, optimize_teacher_days,
                        solve)


@dataclass(frozen=True)
class ObjectiveWeights:
    """Cost per unit of each soft-objective term. Higher = worse.

    The v1 lexicographic order is expressed as dominating magnitudes via
    :meth:`lexicographic`. Schools that trade objectives off against each
    other set comparable weights instead.
    """

    student_double_day: float = 0.0   # per (student, day) with 2 lessons
    teacher_slot_spread: float = 0.0  # per lesson of max-min load spread
    teacher_working_day: float = 0.0  # per (teacher, day) worked
    teacher_day_spread: float = 0.0   # per day of max-min day-count spread
    changed_lesson: float = 0.0       # per lesson differing from a
    #                                   reference schedule (rescheduling)

    @classmethod
    def lexicographic(cls) -> "ObjectiveWeights":
        """Weights that reproduce v1's strict priority order."""
        return cls(student_double_day=1_000_000.0,
                   teacher_slot_spread=10_000.0,
                   teacher_working_day=100.0,
                   teacher_day_spread=1.0)


@dataclass(frozen=True)
class SolverConfig:
    """Everything tunable about a solve, in one place."""

    teacher_capacity: int = 2         # H5: simultaneous students/teacher
    student_day_cap: int = 2          # H8: max lessons per student-day
    require_consecutive: bool = True  # H8: two-a-day must be adjacent
    weights: ObjectiveWeights = field(
        default_factory=ObjectiveWeights.lexicographic)
    time_limit_seconds: float = 10.0  # budget for the optimizing backend
    random_seed: int = 0              # fixed for determinism


def weighted_cost(data: Dataset, lessons: list[Lesson],
                  config: SolverConfig,
                  reference: list[Lesson] | None = None) -> float:
    """Single scalar cost shared by all backends (Phase 1).

    Will subsume ``scheduler.schedule_objective``: each objective term is
    computed as today, then combined with ``config.weights`` instead of
    tuple comparison. ``reference`` feeds the changed-lesson term for
    minimal-disruption rescheduling.
    """
    raise NotImplementedError("solver v2 phase 1 — not implemented yet")


def solve_v2(data: Dataset, config: SolverConfig | None = None,
             fixed_lessons: list[Lesson] | None = None,
             reference: list[Lesson] | None = None) -> SolveResult:
    """Tailored solve: optimize the weighted cost directly (Phase 2).

    Contract:
    * tries the best available backend (CP-SAT when ``ortools`` is
      importable, otherwise the v1 pipeline);
    * ALWAYS validates the winning schedule with ``scheduler.validate``
      and falls back to the v1 result if the backend misbehaves;
    * never returns a worse ``weighted_cost`` than the v1 pipeline.

    Until Phase 2 lands this simply runs the v1 pipeline, so callers can
    already program against the final signature.
    """
    config = config or SolverConfig()
    result = solve(data, fixed_lessons=fixed_lessons,
                   teacher_capacity=config.teacher_capacity)
    if result.complete:
        pinned = list(fixed_lessons or [])
        movable = [l for l in result.lessons if l not in pinned]
        result.lessons = optimize_teacher_days(
            data, movable, fixed=pinned,
            teacher_capacity=config.teacher_capacity)
    # Phase 2 will re-validate backend output here (validate == []) and
    # fall back to this v1 result when the backend misbehaves.
    return result


def _solve_cpsat(data: Dataset, config: SolverConfig,
                 fixed_lessons: list[Lesson],
                 reference: list[Lesson] | None) -> SolveResult | None:
    """CP-SAT backend (Phase 2). Returns None when ortools is missing or
    the time budget produced nothing better than the v1 answer.

    Model sketch (see docs/solver-v2-plan.md):
    * bool var per feasible (need-session, teacher, slot, room) tuple,
      pre-filtered by availability/capability;
    * coverage == need, student ≤1/slot, teacher ≤capacity/slot,
      room ≤capacity/slot, student ≤day-cap/day, adjacency implications,
      pinned lessons fixed;
    * minimize the linearized ``weighted_cost``.
    """
    raise NotImplementedError("solver v2 phase 2 — not implemented yet")


def resolve_minimal_disruption(data: Dataset, current: list[Lesson],
                               config: SolverConfig | None = None
                               ) -> SolveResult:
    """Rescheduling after mid-term input changes (Phase 3).

    Re-solves with ``current`` as the reference schedule and a high
    ``changed_lesson`` weight, so the result stays valid under the new
    inputs while moving as few lessons as possible.
    """
    raise NotImplementedError("solver v2 phase 3 — not implemented yet")
