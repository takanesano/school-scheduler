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

from .scheduler import (OBJECTIVE_TERMS, Dataset, Lesson, SolveCancelled,
                        SolveResult, _slot_sort_key, coverage_report,
                        eligible_teachers, hard_pair_teachers,
                        objective_term_values, optimize_teacher_days,
                        pair_miss_points, slot_penalty_points,
                        subject_buckets, solve, validate)


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
    student_teacher_pair: float = 0.0  # per pair-miss POINT (see
    #                                    scheduler.pair_miss_points)
    teacher_slot_spread: float = 0.0  # per lesson of max-min load spread
    teacher_working_day: float = 0.0  # per (teacher, day) worked
    teacher_single_day: float = 0.0   # per (teacher, day) with at most
    #                                   SolverConfig.single_day_max lessons
    teacher_day_spread: float = 0.0   # per day of max-min day-count spread
    slot_penalty: float = 0.0         # per timeslot-penalty POINT (see
    #                                    scheduler.slot_penalty_points)
    student_subject_repeat: float = 0.0   # per extra same-subject lesson
    #                                       on one (student, day)
    teacher_idle_gap: float = 0.0     # per idle period inside a
    #                                   teacher's working day
    student_subject_spread: float = 0.0   # per over-quota session in a
    #                                       term bucket (see
    #                                       scheduler.subject_buckets)
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
        # ratio 100 between the top ranks, 10 between the bottom ones:
        # the top must stay <= ~1e14 so CP-SAT's int64 objective cannot
        # overflow on big terms (coefficient x hundreds of vars), and
        # dominance matters most at the top of the user's ordering
        magnitudes = [100_000_000_000_000.0, 1_000_000_000_000.0,
                      10_000_000_000.0, 100_000_000.0, 1_000_000.0,
                      100_000.0, 10_000.0, 1_000.0, 100.0, 10.0, 1.0]
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
    # CP-SAT budget. With num_workers == 1 (the default),
    # deterministic_time is the primary cutoff: it is measured in
    # CP-SAT's reproducible work units, so the same input always stops
    # at the same point → identical schedules run-to-run even when
    # optimality is not proven; time_limit_seconds is only a wall-clock
    # safety net. With num_workers > 1 the search runs a parallel
    # portfolio bounded by time_limit_seconds (wall clock) instead —
    # dramatically stronger on big instances (presolve alone exhausts a
    # single worker's budget there), at the price of run-to-run
    # reproducibility. The solve_v2 gate applies either way, so the
    # result is never worse than v1's.
    deterministic_time: float = 8.0
    time_limit_seconds: float = 60.0
    num_workers: int = 1
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
                 fixed_lessons: list[Lesson] | None,
                 should_stop=None,
                 time_limit: float | None = None) -> SolveResult:
    import time as _time
    t0 = _time.monotonic()
    result = solve(data, fixed_lessons=fixed_lessons,
                   teacher_capacity=config.teacher_capacity,
                   student_day_cap=config.student_day_cap,
                   require_consecutive=config.require_consecutive,
                   should_stop=should_stop,
                   time_limit=time_limit)
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
            single_day_max=config.single_day_max,
            should_stop=should_stop,
            time_limit=(max(0.0, time_limit - (_time.monotonic() - t0))
                        if time_limit is not None else None))
    return result


def solve_v2(data: Dataset, config: SolverConfig | None = None,
             fixed_lessons: list[Lesson] | None = None,
             reference: list[Lesson] | None = None,
             incumbent: list[Lesson] | None = None,
             cancel=None) -> SolveResult:
    """Optimize the weighted cost directly; never worse than what the
    user already has.

    ``incumbent`` is the schedule that existed when the user hit
    generate (manually built, or produced by an earlier run of either
    solver). When it is fully valid and covers every need it becomes
    the bar to beat: it warm-starts CP-SAT when it is better than the
    fresh v1 attempt, and the best of {CP answer, incumbent, fresh v1}
    by ``weighted_cost`` is returned — ties prefer the incumbent, so a
    re-generate never reshuffles a schedule it cannot improve.
    ``SolveResult.backend`` says which one won ("cpsat" / "current" /
    "v1") and ``SolveResult.v2_outcome`` says WHY (proved optimal,
    improved, nothing better found, no solution in budget, rules
    infeasible, …).
    """
    config = config or SolverConfig()
    should_stop = (cancel.event.is_set if cancel is not None else None)
    if should_stop and should_stop():
        raise SolveCancelled()
    pinned = list(fixed_lessons or [])
    # the warm start must never become the bottleneck of an exact run:
    # give the v1 pipeline at most a quarter of the budget, capped
    v1_budget = min(10.0, max(2.0, config.time_limit_seconds * 0.25))
    v1 = _v1_pipeline(data, config, pinned, should_stop=should_stop,
                      time_limit=v1_budget)

    def fully_valid(lessons):
        return not (validate(data, lessons, config.teacher_capacity,
                             config.student_day_cap,
                             config.require_consecutive,
                             config.objective_caps,
                             single_day_max=config.single_day_max)
                    or coverage_report(data, lessons))

    v1_usable = v1.complete and fully_valid(v1.lessons)
    v1_cost = (weighted_cost(data, v1.lessons, config, reference)
               if v1_usable else None)
    inc_usable = bool(incumbent) and fully_valid(incumbent)
    inc_cost = (weighted_cost(data, incumbent, config, reference)
                if inc_usable else None)

    # warm start: the reference schedule (when rescheduling), else the
    # best schedule already known — CP-SAT then spends its whole budget
    # improving on it
    if reference is not None:
        hint = reference
    elif inc_usable and (v1_cost is None or inc_cost <= v1_cost):
        hint = incumbent
    else:
        hint = v1.lessons
    cp, cp_state = _solve_cpsat(data, config, pinned, reference, hint,
                                cancel=cancel)
    if should_stop and should_stop():
        raise SolveCancelled()
    if cp is not None and not fully_valid(cp.lessons):
        cp, cp_state = None, "invalid_output"   # backend misbehaved
    cp_cost = (weighted_cost(data, cp.lessons, config, reference)
               if cp is not None else None)

    # pick the cheapest usable schedule; ties prefer the incumbent
    # (no gratuitous reshuffling), then the CP answer
    candidates = []
    if inc_usable:
        candidates.append((inc_cost, 0, "current"))
    if cp is not None:
        candidates.append((cp_cost, 1, "cpsat"))
    if v1_usable:
        candidates.append((v1_cost, 2, "v1"))
    if not candidates:
        v1.v2_outcome = cp_state       # best effort: v1's partial answer
        return v1
    _, _, winner = min(candidates)
    prev_best = min((c for c in (inc_cost, v1_cost) if c is not None),
                    default=None)

    if winner == "cpsat":
        if cp_state == "optimal":
            cp.v2_outcome = "optimal"          # proved best possible
        elif prev_best is None or cp_cost < prev_best:
            cp.v2_outcome = "improved"
        else:
            cp.v2_outcome = "no_improvement"
        return cp
    if winner == "current":
        outcome = ("optimal" if (cp is not None and cp_state == "optimal"
                                 and cp_cost == inc_cost)
                   else "kept_current" if cp is not None
                   else cp_state)     # budget/infeasible/unavailable/…
        return SolveResult(list(incumbent), [], complete=True,
                           backend="current", v2_outcome=outcome)
    v1.v2_outcome = "kept_v1" if cp is not None else cp_state
    return v1


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
                 hint: list[Lesson] | None = None,
                 cancel=None) -> tuple[SolveResult | None, str]:
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

    # H11 hard whitelists and soft assignment-miss weights
    hard_pairs = hard_pair_teachers(data)
    pair_of: dict[str, set] = {}
    strength: dict[str, int] = {}
    for (st, t), k in data.teacher_students.items():
        pair_of.setdefault(st, set()).add(t)
        if k > 0:
            strength[st] = min(strength.get(st, 9), k)

    def miss_weight(st, t):
        ts = pair_of.get(st)
        if ts and t not in ts:
            return 10 - strength.get(st, 9)
        return 0

    m = cp_model.CpModel()
    x: dict[tuple[str, str, str, str, str], object] = {}
    for (st, su), rem in remaining.items():
        combo_vars = []
        allowed = hard_pairs.get(st)
        for s in slots:
            if (st, s.id) not in data.student_availability:
                continue
            for t in teachers_for.get(su, []):
                if allowed is not None and t not in allowed:
                    continue           # H11: assigned teacher only
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
    pin_ssd: Counter = Counter()       # (student, subject, date)
    pin_tdp: Counter = Counter()       # (teacher, date, period)
    for l in pinned:
        slot = data.timeslots.get(l.timeslot_id)
        if slot:
            pin_sdp[(l.student_id, slot.date, slot.period)] += 1
            pin_teacher_day[(l.teacher_id, slot.date)] += 1
            pin_ssd[(l.student_id, l.subject_id, slot.date)] += 1
            pin_tdp[(l.teacher_id, slot.date, slot.period)] += 1

    # ---- hint-derived values for every AUXILIARY variable.
    # CP-SAT treats a hint as one candidate assignment: if any variable
    # is missing it runs a completion search, and on large models it
    # abandons that search — silently discarding the warm start. So
    # every indicator/bound variable created below gets its value under
    # the hint schedule too (the x-variable hints are added at the end).
    hint_td: Counter = Counter()            # (teacher, date) -> load
    hint_day_periods: dict[tuple[str, str], list[int]] = defaultdict(list)
    hint_rst: set[tuple[str, str, str]] = set()
    hint_tload: Counter = Counter()         # teacher -> total lessons
    hint_ssd: Counter = Counter()           # (student, subject, date)
    hint_tdp: Counter = Counter()           # (teacher, date, period)
    if hint is not None:
        for l in hint:
            slot = data.timeslots.get(l.timeslot_id)
            if slot is None:
                continue
            hint_td[(l.teacher_id, slot.date)] += 1
            hint_day_periods[(l.student_id, slot.date)].append(slot.period)
            hint_rst.add((l.room_id, l.timeslot_id, l.teacher_id))
            hint_tload[l.teacher_id] += 1
            hint_ssd[(l.student_id, l.subject_id, slot.date)] += 1
            hint_tdp[(l.teacher_id, slot.date, slot.period)] += 1

    def hint_aux(var, value):
        if hint is not None:
            m.AddHint(var, value)

    # ---- variable indexes
    by_student_slot = defaultdict(list)
    by_teacher_slot = defaultdict(list)
    by_room_slot = defaultdict(list)
    by_room_slot_teacher = defaultdict(list)   # H9: (room, slot, teacher)
    by_sdp = defaultdict(list)         # (student, date, period)
    by_teacher_day = defaultdict(list)
    by_teacher = defaultdict(list)
    by_ssd = defaultdict(list)         # (student, subject, date)
    by_tdp = defaultdict(list)         # (teacher, date, period)
    for (st, su, sid, t, r), v in x.items():
        slot = data.timeslots[sid]
        by_student_slot[(st, sid)].append(v)
        by_teacher_slot[(t, sid)].append(v)
        by_room_slot[(r, sid)].append(v)
        by_room_slot_teacher[(r, sid, t)].append(v)
        by_sdp[(st, slot.date, slot.period)].append(v)
        by_teacher_day[(t, slot.date)].append(v)
        by_teacher[t].append(v)
        by_ssd[(st, su, slot.date)].append(v)
        by_tdp[(t, slot.date, slot.period)].append(v)

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
            # aggregate encoding: one constraint per (room, slot,
            # teacher) instead of one per lesson variable — sum >= 1
            # forces y just as hard, at a fraction of the model size
            vs = by_room_slot_teacher[(r, sid, t)]
            m.Add(sum(vs) <= config.teacher_capacity * y)
            hint_aux(y, 1 if (r, sid, t) in hint_rst else 0)
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
        h_ps = sorted(set(hint_day_periods.get((st, date), [])))
        if gd is not None:
            hint_aux(gd, 1 if (len(h_ps) >= 2
                               and h_ps[-1] - h_ps[0] != len(h_ps) - 1)
                     else 0)
            gd_vars.append(gd)
        if w.student_double_day or "student_double_day" in caps:
            dd = m.NewBoolVar(f"dd[{st},{date}]")
            # total ≥ 2 forces dd = 1
            m.Add(total <= 1 + (config.student_day_cap - 1) * dd)
            hint_aux(dd, 1 if len(hint_day_periods.get((st, date), []))
                     >= 2 else 0)
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
        # H10: this teacher's own daily lesson cap (0/absent = no limit)
        tdm = data.teacher_day_max.get(t, 0)
        if tdm:
            m.Add(load <= tdm)
        if pin_teacher_day.get((t, date), 0):
            m.Add(wd == 1)
        h_load = hint_td.get((t, date), 0)
        hint_aux(wd, 1 if h_load else 0)
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
            hint_aux(sd, 1 if 1 <= h_load <= k else 0)
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
    if w.student_teacher_pair or "student_teacher_pair" in caps:
        miss_terms = [(miss_weight(st, t), v)
                      for (st, su, sid, t, r), v in sorted(x.items())
                      if miss_weight(st, t)]
        if w.student_teacher_pair:
            obj += [int(round(w.student_teacher_pair)) * mw * v
                    for (mw, v) in miss_terms]
        if "student_teacher_pair" in caps:
            pinned_pts = pair_miss_points(data, pinned)
            m.Add(sum(mw * v for (mw, v) in miss_terms)
                  <= caps["student_teacher_pair"] - pinned_pts)
    if w.slot_penalty or "slot_penalty" in caps:
        # like pair-miss: direct coefficients on the lesson variables,
        # one per lesson placed in a penalized slot
        pen_terms = [(data.timeslots[sid].penalty, v)
                     for (st, su, sid, t, r), v in sorted(x.items())
                     if data.timeslots[sid].penalty]
        if w.slot_penalty:
            obj += [int(round(w.slot_penalty)) * pw * v
                    for (pw, v) in pen_terms]
        if "slot_penalty" in caps:
            pinned_pen = slot_penalty_points(data, pinned)
            m.Add(sum(pw * v for (pw, v) in pen_terms)
                  <= caps["slot_penalty"] - pinned_pen)
    if w.student_subject_repeat or "student_subject_repeat" in caps:
        # rep >= (same-subject lessons on the day) - 1, minimized: one
        # IntVar per (student, subject, date) that can actually repeat
        rep_vars = []
        for key in sorted(set(by_ssd) | set(pin_ssd)):
            (st, su, _date) = key
            vs = by_ssd.get(key, [])
            ub = min(len(vs), remaining.get((st, su), 0)) \
                + pin_ssd.get(key, 0)
            if ub < 2:
                continue               # can never have two on this day
            rep = m.NewIntVar(0, ub - 1, f"rep[{','.join(key)}]")
            m.Add(sum(vs) + pin_ssd.get(key, 0) - 1 <= rep)
            hint_aux(rep, max(0, hint_ssd.get(key, 0) - 1))
            rep_vars.append(rep)
        if w.student_subject_repeat:
            obj += [int(round(w.student_subject_repeat)) * v
                    for v in rep_vars]
        if "student_subject_repeat" in caps:
            m.Add(sum(rep_vars) <= caps["student_subject_repeat"])
    if w.teacher_idle_gap or "teacher_idle_gap" in caps:
        # one idle indicator per possible hole period inside a teacher
        # day: idle >= before + after - busy(hole) - 1, all minimized.
        # before/after are one-directional ORs of the busy indicators
        # (>= each member is enough — the objective pushes them down).
        idle_vars = []
        td_periods: dict[tuple[str, str], set[int]] = defaultdict(set)
        for (t, date, p) in by_tdp:
            td_periods[(t, date)].add(p)
        for (t, date, p) in pin_tdp:
            td_periods[(t, date)].add(p)
        for (t, date) in sorted(td_periods):
            ps = sorted(td_periods[(t, date)])
            if ps[-1] - ps[0] < 2:
                continue               # no room for a hole
            busy = {}
            for p in ps:
                load = (sum(by_tdp.get((t, date, p), []))
                        + pin_tdp.get((t, date, p), 0))
                b = m.NewBoolVar(f"tb[{t},{date},{p}]")
                m.Add(load <= config.teacher_capacity * b)
                m.Add(b <= load)
                hint_aux(b, 1 if hint_tdp.get((t, date, p), 0) else 0)
                busy[p] = b
            h_busy = {p for p in ps if hint_tdp.get((t, date, p), 0)}
            for h in range(ps[0] + 1, ps[-1]):
                lo = [busy[p] for p in ps if p < h]
                hi = [busy[q] for q in ps if q > h]
                if not lo or not hi:
                    continue
                before = m.NewBoolVar(f"tbef[{t},{date},{h}]")
                after = m.NewBoolVar(f"taft[{t},{date},{h}]")
                for v in lo:
                    m.Add(before >= v)
                for v in hi:
                    m.Add(after >= v)
                h_lo = any(p in h_busy for p in ps if p < h)
                h_hi = any(q in h_busy for q in ps if q > h)
                hint_aux(before, 1 if h_lo else 0)
                hint_aux(after, 1 if h_hi else 0)
                idle = m.NewBoolVar(f"tidle[{t},{date},{h}]")
                hole_busy = busy.get(h, 0)
                m.Add(before + after - hole_busy - 1 <= idle)
                hint_aux(idle, 1 if (h_lo and h_hi
                                     and h not in h_busy) else 0)
                idle_vars.append(idle)
        if w.teacher_idle_gap:
            obj += [int(round(w.teacher_idle_gap)) * v for v in idle_vars]
        if "teacher_idle_gap" in caps:
            m.Add(sum(idle_vars) <= caps["teacher_idle_gap"])
    if w.student_subject_spread or "student_subject_spread" in caps:
        # over-quota sessions per term bucket (scheduler.subject_buckets
        # defines the buckets; quota = ceil(sessions / buckets)), same
        # over >= count - quota pattern as the repeat term
        over_vars = []
        by_subj_date: dict[tuple[str, str], dict[str, list]] = \
            defaultdict(lambda: defaultdict(list))
        for (st, su, date), vs in by_ssd.items():
            by_subj_date[(st, su)][date].extend(vs)
        subj_keys = sorted(set(by_subj_date)
                           | {(st, su) for (st, su, _d) in pin_ssd})
        for (st, su) in subj_keys:
            n = data.student_needs.get((st, su), 0)
            if n < 2:
                continue
            buckets = subject_buckets(data, st, n)
            k = max(buckets.values(), default=0) + 1
            if k < 2:
                continue               # a single bucket can never overflow
            quota = -(-n // k)
            bucket_vars: dict[int, list] = defaultdict(list)
            for date, vs in by_subj_date.get((st, su), {}).items():
                bucket_vars[buckets.get(date, 0)].extend(vs)
            pin_b: Counter = Counter()
            hint_b: Counter = Counter()
            for (pst, psu, pdate), c in pin_ssd.items():
                if (pst, psu) == (st, su):
                    pin_b[buckets.get(pdate, 0)] += c
            for (hst, hsu, hdate), c in hint_ssd.items():
                if (hst, hsu) == (st, su):
                    hint_b[buckets.get(hdate, 0)] += c
            for b in range(k):
                vs = bucket_vars.get(b, [])
                ub = min(len(vs), n) + pin_b.get(b, 0)
                if ub <= quota:
                    continue           # can never exceed the quota
                over = m.NewIntVar(0, ub - quota,
                                   f"sspr[{st},{su},{b}]")
                m.Add(sum(vs) + pin_b.get(b, 0) - quota <= over)
                hint_aux(over, max(0, hint_b.get(b, 0) - quota))
                over_vars.append(over)
        if w.student_subject_spread:
            obj += [int(round(w.student_subject_spread)) * v
                    for v in over_vars]
        if "student_subject_spread" in caps:
            m.Add(sum(over_vars) <= caps["student_subject_spread"])
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
        h_loads = [hint_tload.get(t, 0) for t in elig]
        hint_aux(lmax, max(h_loads, default=0))
        hint_aux(lmin, min(h_loads, default=0))
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
        h_days = [sum(1 for (t2, _d), n in hint_td.items()
                      if t2 == t and n) for t in elig]
        hint_aux(dmax, max(h_days, default=0))
        hint_aux(dmin, min(h_days, default=0))
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
        # hint EVERY lesson variable (1 for hinted lessons, 0 otherwise;
        # the auxiliary indicators were hinted at their creation sites):
        # a complete hint is validated directly, while a partial one
        # needs a completion search that CP-SAT abandons on big models —
        # silently discarding the warm start
        hint_keys = {(l.student_id, l.subject_id, l.timeslot_id,
                      l.teacher_id, l.room_id) for l in hint}
        for key, var in sorted(x.items()):
            m.AddHint(var, 1 if key in hint_keys else 0)

    if cancel is not None and cancel.event.is_set():
        raise SolveCancelled()
    solver = cp_model.CpSolver()
    if cancel is not None:
        # expose the live solver so /api/schedule/cancel can call
        # stop_search() on it from another thread
        cancel.cp = solver
    workers = config.num_workers
    if workers > 1 and config.time_limit_seconds < 5:
        # a wall deadline shorter than the portfolio's startup can kill
        # workers mid-initialization — ortools then aborts the whole
        # PROCESS (native CHECK failure), intermittently. Tiny budgets
        # gain nothing from parallelism anyway: run single-threaded.
        workers = 1
    solver.parameters.num_workers = workers
    if config.num_workers == 1:
        # deterministic mode: the reproducible work-unit budget binds
        solver.parameters.max_deterministic_time = config.deterministic_time
    solver.parameters.max_time_in_seconds = config.time_limit_seconds
    solver.parameters.random_seed = config.random_seed
    solver.parameters.repair_hint = True   # for slightly-stale hints
    status = solver.Solve(m)
    if cancel is not None:
        cancel.cp = None
        if cancel.event.is_set():
            raise SolveCancelled()
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
