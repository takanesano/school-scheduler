"""Solver v2 — exact, weight-driven schedule optimization via CP-SAT.

Where v1 (``scheduler.solve`` + ``optimize_teacher_days``) finds *a* legal
schedule and then hill-climbs, v2 models the whole problem as a constraint
program and optimizes a single weighted objective directly with OR-tools
CP-SAT. See docs/solver-v2-plan.md for the design rationale.

Model sketch
------------
* One boolean variable per feasible (student, subject, timeslot, teacher,
  room) assignment, pre-filtered by availability and capability. Sessions
  of one (student, subject) need are interchangeable, so coverage is a
  plain sum-equality — no symmetric per-session variables.
* Hard constraints (mirroring the validator's H1–H8): coverage == need,
  student ≤ 1 per slot, teacher ≤ capacity per slot, room ≤ capacity per
  slot, student ≤ day-cap per day, and pairwise "no two non-adjacent
  periods on one student-day" for consecutiveness.
* Pinned lessons (user-placed, ``fixed_lessons``) are constants folded
  into every constraint, never variables — they cannot move.
* Soft objectives from ``SolverConfig.weights``: two-lesson-day
  indicators, teacher working-day indicators, max−min load and day-count
  spreads over eligible teachers, and (for rescheduling) a penalty per
  lesson changed from a reference schedule.

Safety contract
---------------
``solve_v2`` ALWAYS runs the v1 pipeline too, and only returns the CP-SAT
answer when it (a) passes ``scheduler.validate`` and ``coverage_report``
cleanly and (b) has a weighted cost no worse than v1's. If OR-tools is
not installed, the model is infeasible (e.g. force-saved pinned lessons
already break a rule), or the time budget runs out, the v1 result is
returned unchanged. Determinism: fixed seed, one worker, sorted model
construction.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace

from .scheduler import (OBJECTIVE_TERMS, Dataset, Lesson, SolveResult,
                        _slot_sort_key, coverage_report, eligible_teachers,
                        objective_term_values, optimize_teacher_days, solve,
                        validate)


@dataclass(frozen=True)
class ObjectiveWeights:
    """Cost per unit of each soft-objective term. Higher = worse.

    The v1 lexicographic order is expressed as dominating magnitudes via
    :meth:`lexicographic`. Schools that trade objectives off against each
    other set comparable weights instead. CP-SAT needs integer
    coefficients, so weights are rounded to ints inside the model.
    """

    student_double_day: float = 0.0   # per (student, day) with 2 lessons
    student_day_gap: float = 0.0      # per (student, day) non-contiguous
    teacher_slot_spread: float = 0.0  # per lesson of max-min load spread
    teacher_working_day: float = 0.0  # per (teacher, day) worked
    teacher_single_day: float = 0.0   # per (teacher, day) with at most
    #                                   SolverConfig.single_day_max lessons
    teacher_day_spread: float = 0.0   # per day of max-min day-count spread
    changed_lesson: float = 0.0       # per lesson differing from a
    #                                   reference schedule (rescheduling)

    @classmethod
    def lexicographic(cls, order: list[str] | None = None
                      ) -> "ObjectiveWeights":
        """Weights giving a strict priority order (default: v1's).

        ``order`` is a permutation of ``scheduler.OBJECTIVE_TERMS``,
        most important first; each rank's weight dominates everything
        below it combined.
        """
        order = list(order or OBJECTIVE_TERMS)
        if sorted(order) != sorted(OBJECTIVE_TERMS):
            raise ValueError(
                f"order must be a permutation of {OBJECTIVE_TERMS}")
        magnitudes = [10_000_000_000.0, 100_000_000.0, 1_000_000.0,
                      10_000.0, 100.0, 1.0]
        return cls(**{name: magnitudes[i] for i, name in enumerate(order)})


@dataclass(frozen=True)
class SolverConfig:
    """Everything tunable about a solve, in one place."""

    teacher_capacity: int = 2         # H5: simultaneous students/teacher
    student_day_cap: int = 2          # H8: max lessons per student-day
    require_consecutive: bool = True  # H8: a student's day is contiguous
    # teacher_single_day counts worked days with at most this many lessons
    single_day_max: int = 1
    # soft objectives promoted to hard constraints: term name -> max value
    objective_caps: dict[str, int] | None = None
    weights: ObjectiveWeights = field(
        default_factory=ObjectiveWeights.lexicographic)
    # CP-SAT budget. deterministic_time is the primary cutoff: it is
    # measured in CP-SAT's reproducible work units, so the same input
    # always stops at the same point → identical schedules run-to-run
    # even when optimality is not proven. time_limit_seconds is only a
    # wall-clock safety net.
    deterministic_time: float = 8.0
    time_limit_seconds: float = 60.0
    random_seed: int = 0              # fixed for determinism


def objective_terms(data: Dataset, lessons: list[Lesson],
                    reference: list[Lesson] | None = None,
                    single_day_max: int = 1) -> dict[str, int]:
    """The named objective terms, evaluated on a concrete schedule."""
    changed = 0
    if reference is not None:
        key = (lambda l: (l.student_id, l.subject_id, l.teacher_id,
                          l.room_id, l.timeslot_id))
        changed = sum((Counter(map(key, reference))
                       - Counter(map(key, lessons))).values())
    return {**objective_term_values(data, lessons,
                                    single_day_max=single_day_max),
            "changed_lesson": changed}


def weighted_cost(data: Dataset, lessons: list[Lesson],
                  config: SolverConfig,
                  reference: list[Lesson] | None = None) -> float:
    """Single scalar cost shared by every backend."""
    terms = objective_terms(data, lessons, reference,
                            single_day_max=config.single_day_max)
    return sum(getattr(config.weights, name) * value
               for name, value in terms.items())


def _v1_pipeline(data: Dataset, config: SolverConfig,
                 fixed_lessons: list[Lesson] | None) -> SolveResult:
    result = solve(data, fixed_lessons=fixed_lessons,
                   teacher_capacity=config.teacher_capacity,
                   student_day_cap=config.student_day_cap,
                   require_consecutive=config.require_consecutive)
    if result.complete:
        pinned = list(fixed_lessons or [])
        movable = [l for l in result.lessons if l not in pinned]
        # hill-climb in the priority order the weights imply, so the v1
        # fallback honors a custom objective_order too
        order = sorted(OBJECTIVE_TERMS,
                       key=lambda n: -getattr(config.weights, n))
        result.lessons = optimize_teacher_days(
            data, movable, fixed=pinned,
            teacher_capacity=config.teacher_capacity,
            student_day_cap=config.student_day_cap,
            require_consecutive=config.require_consecutive,
            objective_order=order,
            single_day_max=config.single_day_max)
    return result


def solve_v2(data: Dataset, config: SolverConfig | None = None,
             fixed_lessons: list[Lesson] | None = None,
             reference: list[Lesson] | None = None) -> SolveResult:
    """Optimize the weighted cost directly; never worse than v1.

    Returns the CP-SAT schedule only when it is fully valid, covers every
    need exactly, and its ``weighted_cost`` is ≤ the v1 pipeline's.
    Otherwise (OR-tools missing, model infeasible — e.g. force-saved
    pinned lessons already violate a rule — or time budget too small) the
    v1 result is returned; ``SolveResult.backend`` says which one won
    and ``SolveResult.v2_outcome`` says WHY (proved optimal, improved,
    nothing better found, no solution in budget, rules infeasible, …).
    """
    config = config or SolverConfig()
    pinned = list(fixed_lessons or [])
    v1 = _v1_pipeline(data, config, pinned)
    # warm start: the reference schedule (when rescheduling) or v1's
    # answer — CP-SAT then spends its whole budget improving, and a
    # timeout can rarely be worse than v1
    hint = reference if reference is not None else v1.lessons
    cp, cp_state = _solve_cpsat(data, config, pinned, reference, hint)
    if cp is None:
        v1.v2_outcome = cp_state       # unavailable / input_problem /
        return v1                      # infeasible / no_solution_in_budget

    def fully_valid(lessons):
        return not (validate(data, lessons, config.teacher_capacity,
                             config.student_day_cap,
                             config.require_consecutive,
                             config.objective_caps,
                             single_day_max=config.single_day_max)
                    or coverage_report(data, lessons))
    if not fully_valid(cp.lessons):
        v1.v2_outcome = "invalid_output"
        return v1                      # backend misbehaved: v1 wins
    # prefer v1 on cost only when v1 itself satisfies everything —
    # including promoted objective caps, which v1 cannot enforce
    v1_usable = v1.complete and fully_valid(v1.lessons)
    cp_cost = weighted_cost(data, cp.lessons, config, reference)
    v1_cost = (weighted_cost(data, v1.lessons, config, reference)
               if v1_usable else None)
    if v1_usable and cp_cost > v1_cost:
        v1.v2_outcome = "kept_v1"      # budget produced nothing better
        return v1
    if cp_state == "optimal":
        cp.v2_outcome = "optimal"      # proved best possible
    elif v1_usable and cp_cost == v1_cost:
        cp.v2_outcome = "no_improvement"
    else:
        cp.v2_outcome = "improved"
    return cp


def resolve_minimal_disruption(data: Dataset, current: list[Lesson],
                               config: SolverConfig | None = None
                               ) -> SolveResult:
    """Reschedule after mid-term input changes, moving as little as
    possible.

    Re-solves with ``current`` as the reference schedule; every changed
    lesson costs ``weights.changed_lesson`` (raised to a dominating value
    when left at 0), so the result stays valid under the new inputs while
    preserving as much of ``current`` as it can. Requires OR-tools — the
    v1 fallback cannot honor the reference and simply re-solves.
    """
    config = config or SolverConfig()
    if config.weights.changed_lesson <= 0:
        config = replace(config, weights=replace(
            config.weights, changed_lesson=100_000_000.0))
    return solve_v2(data, config=config, reference=current)


def _solve_cpsat(data: Dataset, config: SolverConfig,
                 pinned: list[Lesson],
                 reference: list[Lesson] | None,
                 hint: list[Lesson] | None = None
                 ) -> tuple[SolveResult | None, str]:
    """Build and solve the CP-SAT model.

    Returns (result, state); result None = defer to v1, with state
    saying why: "unavailable" (no ortools), "input_problem",
    "infeasible" (the hard rules admit NO schedule), or
    "no_solution_in_budget" (search ended before finding one). With a
    result, state is "optimal" (proved best) or "feasible" (budget
    ended first)."""
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return None, "unavailable"

    slots = sorted(data.timeslots.values(), key=_slot_sort_key)
    room_ids = sorted(data.rooms)
    teachers_for: dict[str, list[str]] = defaultdict(list)
    for (t, su) in sorted(data.teacher_subjects):
        teachers_for[su].append(t)

    # needs left after pinned lessons are counted
    pinned_count = Counter((l.student_id, l.subject_id) for l in pinned)
    remaining: dict[tuple[str, str], int] = {}
    for (st, su), need in sorted(data.student_needs.items()):
        if st not in data.students or su not in data.subjects:
            return None, "input_problem"   # unschedulable: v1 reports it
        rem = need - pinned_count.get((st, su), 0)
        if rem > 0:
            remaining[(st, su)] = rem

    m = cp_model.CpModel()
    x: dict[tuple[str, str, str, str, str], object] = {}
    for (st, su), rem in remaining.items():
        combo_vars = []
        for s in slots:
            if (st, s.id) not in data.student_availability:
                continue
            for t in teachers_for.get(su, []):
                if (t, s.id) not in data.teacher_availability:
                    continue
                for r in room_ids:
                    v = m.NewBoolVar(f"x[{st},{su},{s.id},{t},{r}]")
                    x[(st, su, s.id, t, r)] = v
                    combo_vars.append(v)
        m.Add(sum(combo_vars) == rem)  # coverage: exactly `sessions`

    # ---- occupancy constants contributed by pinned lessons
    pin_student_slot = Counter((l.student_id, l.timeslot_id) for l in pinned)
    pin_teacher_slot = Counter((l.teacher_id, l.timeslot_id) for l in pinned)
    pin_room_slot = Counter((l.room_id, l.timeslot_id) for l in pinned)
    pin_sdp: Counter = Counter()       # (student, date, period)
    pin_teacher_day: Counter = Counter()
    for l in pinned:
        slot = data.timeslots.get(l.timeslot_id)
        if slot:
            pin_sdp[(l.student_id, slot.date, slot.period)] += 1
            pin_teacher_day[(l.teacher_id, slot.date)] += 1

    # ---- variable indexes
    by_student_slot = defaultdict(list)
    by_teacher_slot = defaultdict(list)
    by_room_slot = defaultdict(list)
    by_room_slot_teacher = defaultdict(list)   # H9: (room, slot, teacher)
    by_sdp = defaultdict(list)         # (student, date, period)
    by_teacher_day = defaultdict(list)
    by_teacher = defaultdict(list)
    for (st, su, sid, t, r), v in x.items():
        slot = data.timeslots[sid]
        by_student_slot[(st, sid)].append(v)
        by_teacher_slot[(t, sid)].append(v)
        by_room_slot[(r, sid)].append(v)
        by_room_slot_teacher[(r, sid, t)].append(v)
        by_sdp[(st, slot.date, slot.period)].append(v)
        by_teacher_day[(t, slot.date)].append(v)
        by_teacher[t].append(v)

    # ---- hard constraints (H5–H8; H1–H4 are enforced by var filtering)
    for (st, sid), vs in sorted(by_student_slot.items()):
        m.Add(sum(vs) <= 1 - pin_student_slot.get((st, sid), 0))
    for (t, sid), vs in sorted(by_teacher_slot.items()):
        m.Add(sum(vs) <= config.teacher_capacity
              - pin_teacher_slot.get((t, sid), 0))
    for (r, sid), vs in sorted(by_room_slot.items()):
        m.Add(sum(vs) <= data.rooms[r].capacity
              - pin_room_slot.get((r, sid), 0))

    # H9: max DISTINCT teachers per (room, slot). A presence bool per
    # teacher is forced up by any of that teacher's lessons there;
    # pinned teachers count as constants.
    pin_room_slot_teachers: dict[tuple[str, str], set[str]] = defaultdict(set)
    for l in pinned:
        pin_room_slot_teachers[(l.room_id, l.timeslot_id)].add(l.teacher_id)
    h9_slots = defaultdict(set)                # (room, slot) -> teachers
    for (r, sid, t) in by_room_slot_teacher:
        h9_slots[(r, sid)].add(t)
    for (r, sid), teachers in sorted(h9_slots.items()):
        tcap = data.rooms[r].teacher_capacity
        if not tcap:
            continue
        pinned_here = pin_room_slot_teachers.get((r, sid), set())
        if len(teachers | pinned_here) <= tcap:
            continue                           # can never exceed the limit
        present = []
        for t in sorted(teachers - pinned_here):
            y = m.NewBoolVar(f"rt[{r},{sid},{t}]")
            for v in by_room_slot_teacher[(r, sid, t)]:
                m.Add(v <= y)
            present.append(y)
        m.Add(sum(present) <= tcap - len(pinned_here))

    # student-day structures: cap and consecutiveness
    day_periods: dict[str, list[int]] = defaultdict(list)
    for s in slots:
        day_periods[s.date].append(s.period)
    student_days = sorted(
        {(st, date) for (st, date, _p) in by_sdp}
        | {(st, date) for (st, date, _p) in pin_sdp})
    dd_vars = []
    gd_vars = []
    w = config.weights
    caps = config.objective_caps or {}
    soft_gap = (not config.require_consecutive
                and (w.student_day_gap or "student_day_gap" in caps))
    for (st, date) in student_days:
        periods = sorted(set(day_periods[date]))

        def occ(p, st=st, date=date):   # occupancy of one period (expr)
            return (sum(by_sdp.get((st, date, p), []))
                    + pin_sdp.get((st, date, p), 0))

        total = sum(occ(p) for p in periods)
        m.Add(total <= config.student_day_cap)
        # contiguity: for p < q non-adjacent, both occupied forces every
        # period in between occupied (impossible if a period in between
        # does not exist that day). Hard mode forbids the violation; soft
        # mode charges it to a gap-day indicator instead.
        gd = m.NewBoolVar(f"gd[{st},{date}]") if soft_gap else None
        present = set(periods)
        if config.require_consecutive or soft_gap:
            for i, p in enumerate(periods):
                for q in periods[i + 1:]:
                    if q - p == 1:
                        continue
                    holes = list(range(p + 1, q))
                    bridged = not any(h not in present for h in holes)
                    if config.require_consecutive:
                        if bridged:
                            for h in holes:
                                m.Add(occ(p) + occ(q) <= 1 + occ(h))
                        else:
                            m.Add(occ(p) + occ(q) <= 1)
                    else:
                        if bridged:
                            for h in holes:
                                m.Add(occ(p) + occ(q) - occ(h) - 1 <= gd)
                        else:
                            m.Add(occ(p) + occ(q) - 1 <= gd)
        if gd is not None:
            gd_vars.append(gd)
        if w.student_double_day or "student_double_day" in caps:
            dd = m.NewBoolVar(f"dd[{st},{date}]")
            # total ≥ 2 forces dd = 1
            m.Add(total <= 1 + (config.student_day_cap - 1) * dd)
            dd_vars.append(dd)

    # teacher working-day indicators, per-teacher day counts, and
    # single-lesson-day indicators (exactly one lesson on a day)
    teacher_days = sorted(set(by_teacher_day) | set(pin_teacher_day))
    wd_vars = []
    sd_vars = []
    need_sd = bool(w.teacher_single_day or "teacher_single_day" in caps)
    day_count_of: dict[str, list] = defaultdict(list)
    for (t, date) in teacher_days:
        wd = m.NewBoolVar(f"wd[{t},{date}]")
        cap_day = config.teacher_capacity * len(set(day_periods[date]))
        load = (sum(by_teacher_day.get((t, date), []))
                + pin_teacher_day.get((t, date), 0))
        m.Add(load <= cap_day * wd)
        if pin_teacher_day.get((t, date), 0):
            m.Add(wd == 1)
        wd_vars.append(wd)
        day_count_of[t].append(wd)
        if need_sd:
            # sd = 1 forced when the day is worked with at most
            # single_day_max lessons (wd=1, 1 <= load <= K ->
            # (K+1)*wd - load >= 1 > K*0); as a side effect wd is
            # forced honest (0) on load-0 days ((K+1)*1 > K*1)
            k = config.single_day_max
            sd = m.NewBoolVar(f"sd[{t},{date}]")
            m.Add((k + 1) * wd - load <= k * sd)
            sd_vars.append(sd)

    # ---- objective terms and promoted hard caps
    total_sessions = sum(remaining.values()) + len(pinned)
    obj = []
    if w.student_double_day:
        obj += [int(round(w.student_double_day)) * dd for dd in dd_vars]
    if "student_double_day" in caps:
        m.Add(sum(dd_vars) <= caps["student_double_day"])
    if w.student_day_gap:
        obj += [int(round(w.student_day_gap)) * gd for gd in gd_vars]
    if "student_day_gap" in caps and not config.require_consecutive:
        m.Add(sum(gd_vars) <= caps["student_day_gap"])
    if w.teacher_working_day:
        obj += [int(round(w.teacher_working_day)) * wd for wd in wd_vars]
    if "teacher_working_day" in caps:
        m.Add(sum(wd_vars) <= caps["teacher_working_day"])
    if w.teacher_single_day:
        obj += [int(round(w.teacher_single_day)) * sd for sd in sd_vars]
    if "teacher_single_day" in caps:
        m.Add(sum(sd_vars) <= caps["teacher_single_day"])
    elig = eligible_teachers(data)
    pin_teacher_total = Counter(l.teacher_id for l in pinned)
    if (w.teacher_slot_spread or "teacher_slot_spread" in caps) and elig:
        lmax = m.NewIntVar(0, total_sessions, "load_max")
        lmin = m.NewIntVar(0, total_sessions, "load_min")
        for t in elig:
            load = sum(by_teacher[t]) + pin_teacher_total.get(t, 0)
            m.Add(lmax >= load)
            m.Add(lmin <= load)
        if w.teacher_slot_spread:
            obj.append(int(round(w.teacher_slot_spread)) * (lmax - lmin))
        if "teacher_slot_spread" in caps:
            m.Add(lmax - lmin <= caps["teacher_slot_spread"])
    if (w.teacher_day_spread or "teacher_day_spread" in caps) and elig:
        n_days = len(day_periods)
        dmax = m.NewIntVar(0, n_days, "days_max")
        dmin = m.NewIntVar(0, n_days, "days_min")
        for t in elig:
            count = sum(day_count_of.get(t, []))
            m.Add(dmax >= count)
            m.Add(dmin <= count)
        if w.teacher_day_spread:
            obj.append(int(round(w.teacher_day_spread)) * (dmax - dmin))
        if "teacher_day_spread" in caps:
            m.Add(dmax - dmin <= caps["teacher_day_spread"])
    if w.changed_lesson and reference:
        seen = set()
        for l in reference:
            key = (l.student_id, l.subject_id, l.timeslot_id,
                   l.teacher_id, l.room_id)
            if key in x and key not in seen:
                seen.add(key)
                obj.append(int(round(w.changed_lesson)) * (1 - x[key]))
            # reference lessons with no matching variable are unavoidably
            # changed — a constant cost that cannot affect the argmin
    m.Minimize(sum(obj) if obj else 0)

    if hint:
        # hint EVERY variable (1 for hinted lessons, 0 otherwise): a
        # complete hint is validated directly, while a partial one needs
        # a completion search that CP-SAT abandons quickly
        hint_keys = {(l.student_id, l.subject_id, l.timeslot_id,
                      l.teacher_id, l.room_id) for l in hint}
        for key, var in sorted(x.items()):
            m.AddHint(var, 1 if key in hint_keys else 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_deterministic_time = config.deterministic_time
    solver.parameters.max_time_in_seconds = config.time_limit_seconds
    solver.parameters.random_seed = config.random_seed
    solver.parameters.num_workers = 1
    solver.parameters.repair_hint = True   # for slightly-stale hints
    status = solver.Solve(m)
    if status == cp_model.INFEASIBLE:
        return None, "infeasible"
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, "no_solution_in_budget"

    lessons = list(pinned)
    for (st, su, sid, t, r), v in sorted(x.items()):
        if solver.Value(v):
            lessons.append(Lesson(st, su, t, r, sid))
    return SolveResult(lessons, [], complete=True,
                       nodes_explored=int(solver.NumBranches()),
                       backend="cpsat"), (
        "optimal" if status == cp_model.OPTIMAL else "feasible")
