"""Timetable domain logic: dataset snapshot, validator, and solver.

This module is pure (no I/O, no DB) so it can be tested exhaustively.

Model
-----
A *lesson* is one session: (student, subject, teacher, room, timeslot).
Timeslots carry concrete calendar dates, so a term (e.g. a summer school)
is scheduled as a whole and every day is unique.

Hard constraints checked by ``validate`` and honoured by ``solve``:
  H1  every referenced entity exists
  H2  the teacher can teach the lesson's subject
  H3  the teacher is available at the timeslot
  H4  the student is available at the timeslot
  H5  a teacher gives at most ``teacher_capacity`` lessons per timeslot
      (default 2: a teacher may teach two students at once, even in
      different subjects)
  H6  a student takes at most one lesson per timeslot
  H7  lessons in a room at one timeslot never exceed the room's capacity
  H8  a student has at most TWO lessons per calendar day, and when there
      are two they occupy consecutive periods (period numbers differing
      by exactly 1)

Soft objectives, applied by ``optimize_teacher_days`` after solving
(lexicographic — each strictly dominates the next):
  O1  as few student-days with two lessons as possible — one lesson per
      day per student as far as possible
  O2  lesson counts are as even as possible across (eligible) teachers —
      no idle teacher next to an overloaded one
  O3  each teacher's lessons are packed into as few distinct days as
      possible
  O4  working-day counts are as even as possible across teachers
"Eligible" teachers are those with at least one teachable subject and one
available timeslot; others cannot take lessons and are ignored by the
spread metrics.

Coverage (reported by ``coverage_report``): every (student, subject) need is
met by exactly ``sessions`` lessons — no more, no fewer.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Timeslot:
    id: str
    date: str          # ISO YYYY-MM-DD — each calendar day is unique
    period: int
    label: str = ""


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    capacity: int = 1


@dataclass(frozen=True)
class Lesson:
    student_id: str
    subject_id: str
    teacher_id: str
    room_id: str
    timeslot_id: str
    id: int | None = None


@dataclass
class Dataset:
    """Immutable-by-convention snapshot of all scheduling inputs."""

    students: dict[str, str] = field(default_factory=dict)   # id -> name
    teachers: dict[str, str] = field(default_factory=dict)   # id -> name
    subjects: dict[str, str] = field(default_factory=dict)   # id -> name
    rooms: dict[str, Room] = field(default_factory=dict)
    timeslots: dict[str, Timeslot] = field(default_factory=dict)
    teacher_subjects: set[tuple[str, str]] = field(default_factory=set)
    student_needs: dict[tuple[str, str], int] = field(default_factory=dict)
    teacher_availability: set[tuple[str, str]] = field(default_factory=set)
    student_availability: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True)
class Violation:
    code: str        # e.g. "teacher_double_booked"
    message: str
    lesson_ids: tuple[int | None, ...] = ()


def _slot_sort_key(ts: Timeslot) -> tuple:
    return (ts.date, ts.period)   # ISO dates sort chronologically


def validate(data: Dataset, lessons: list[Lesson],
             teacher_capacity: int = 2,
             student_day_cap: int = 2,
             require_consecutive: bool = True,
             objective_caps: dict[str, int] | None = None,
             single_day_max: int = 1
             ) -> list[Violation]:
    """Return every hard-constraint violation in the given schedule.

    ``teacher_capacity`` / ``student_day_cap`` / ``require_consecutive``
    parameterize H5 and H8. ``objective_caps`` maps OBJECTIVE_TERMS names
    to upper bounds — soft objectives the user promoted to hard
    constraints; exceeding a bound is reported as a schedule-level
    violation (no specific lesson ids)."""
    out: list[Violation] = []

    def bad(code: str, msg: str, *ids: int | None) -> None:
        out.append(Violation(code, msg, tuple(ids)))

    for l in lessons:
        # H1
        missing = []
        if l.student_id not in data.students:
            missing.append(f"student '{l.student_id}'")
        if l.subject_id not in data.subjects:
            missing.append(f"subject '{l.subject_id}'")
        if l.teacher_id not in data.teachers:
            missing.append(f"teacher '{l.teacher_id}'")
        if l.room_id not in data.rooms:
            missing.append(f"room '{l.room_id}'")
        if l.timeslot_id not in data.timeslots:
            missing.append(f"timeslot '{l.timeslot_id}'")
        if missing:
            bad("unknown_reference",
                f"Lesson references unknown {', '.join(missing)}", l.id)
            continue  # further checks on this lesson would be misleading

        slot = data.timeslots[l.timeslot_id]
        # H2
        if (l.teacher_id, l.subject_id) not in data.teacher_subjects:
            bad("teacher_cannot_teach",
                f"Teacher {data.teachers[l.teacher_id]} cannot teach "
                f"{data.subjects[l.subject_id]}", l.id)
        # H3
        if (l.teacher_id, l.timeslot_id) not in data.teacher_availability:
            bad("teacher_unavailable",
                f"Teacher {data.teachers[l.teacher_id]} is not available at "
                f"{slot.date} P{slot.period}", l.id)
        # H4
        if (l.student_id, l.timeslot_id) not in data.student_availability:
            bad("student_unavailable",
                f"Student {data.students[l.student_id]} is not available at "
                f"{slot.date} P{slot.period}", l.id)

    known = [l for l in lessons
             if l.student_id in data.students and l.subject_id in data.subjects
             and l.teacher_id in data.teachers and l.room_id in data.rooms
             and l.timeslot_id in data.timeslots]

    # H5
    by_teacher_slot: dict[tuple[str, str], list[Lesson]] = defaultdict(list)
    for l in known:
        by_teacher_slot[(l.teacher_id, l.timeslot_id)].append(l)
    for (t, s), ls in sorted(by_teacher_slot.items()):
        if len(ls) > teacher_capacity:
            slot = data.timeslots[s]
            bad("teacher_over_capacity",
                f"Teacher {data.teachers[t]} has {len(ls)} lessons at "
                f"{slot.date} P{slot.period} (max {teacher_capacity} at once)",
                *[l.id for l in ls])

    # H6
    by_student_slot: dict[tuple[str, str], list[Lesson]] = defaultdict(list)
    for l in known:
        by_student_slot[(l.student_id, l.timeslot_id)].append(l)
    for (st, s), ls in sorted(by_student_slot.items()):
        if len(ls) > 1:
            slot = data.timeslots[s]
            bad("student_double_booked",
                f"Student {data.students[st]} has {len(ls)} lessons at "
                f"{slot.date} P{slot.period}", *[l.id for l in ls])

    # H7
    by_room_slot: dict[tuple[str, str], list[Lesson]] = defaultdict(list)
    for l in known:
        by_room_slot[(l.room_id, l.timeslot_id)].append(l)
    for (r, s), ls in sorted(by_room_slot.items()):
        cap = data.rooms[r].capacity
        if len(ls) > cap:
            slot = data.timeslots[s]
            bad("room_over_capacity",
                f"Room {data.rooms[r].name} holds {len(ls)} lessons at "
                f"{slot.date} P{slot.period} (capacity {cap})",
                *[l.id for l in ls])

    # H8: per-student daily cap; multiple lessons must sit in one
    # contiguous run of periods when require_consecutive is on
    by_student_day: dict[tuple[str, str], list[Lesson]] = defaultdict(list)
    for l in known:
        date = data.timeslots[l.timeslot_id].date
        by_student_day[(l.student_id, date)].append(l)
    for (st, date), ls in sorted(by_student_day.items()):
        if len(ls) > student_day_cap:
            bad("student_day_overload",
                f"Student {data.students[st]} has {len(ls)} lessons on "
                f"{date} (max {student_day_cap} per day)",
                *[l.id for l in ls])
        elif require_consecutive and len(ls) >= 2:
            periods = sorted(data.timeslots[l.timeslot_id].period for l in ls)
            # duplicates mean a same-slot clash — H6 already reports that
            if (len(set(periods)) == len(periods)
                    and periods[-1] - periods[0] != len(periods) - 1):
                plist = ", ".join(f"P{p}" for p in periods)
                bad("student_day_gap",
                    f"Student {data.students[st]}'s lessons on {date} "
                    f"({plist}) must be in consecutive periods",
                    *[l.id for l in ls])

    # promoted objectives: aggregate metrics with hard upper bounds.
    # student_day_gap capped at 0 is exactly the require_consecutive rule,
    # which is already reported per student-day (with lesson ids) above —
    # skip it here to avoid double reporting.
    for term, bound in sorted((objective_caps or {}).items()):
        if term == "student_day_gap" and require_consecutive:
            continue
        value = objective_term_values(
            data, known, single_day_max=single_day_max).get(term)
        if value is not None and value > bound:
            bad("objective_cap_exceeded",
                f"{OBJECTIVE_LABELS.get(term, term)} is {value} but must "
                f"be at most {bound}")

    return out


def coverage_report(data: Dataset, lessons: list[Lesson]) -> list[Violation]:
    """Compare scheduled lessons against student needs."""
    out: list[Violation] = []
    scheduled = Counter((l.student_id, l.subject_id) for l in lessons)
    for (st, su), need in sorted(data.student_needs.items()):
        got = scheduled.pop((st, su), 0)
        if got < need:
            out.append(Violation(
                "need_unmet",
                f"Student {data.students.get(st, st)} needs {need} "
                f"{data.subjects.get(su, su)} sessions but has {got}"))
        elif got > need:
            out.append(Violation(
                "need_exceeded",
                f"Student {data.students.get(st, st)} needs {need} "
                f"{data.subjects.get(su, su)} sessions but has {got}"))
    for (st, su), got in sorted(scheduled.items()):
        out.append(Violation(
            "lesson_without_need",
            f"Student {data.students.get(st, st)} has {got} "
            f"{data.subjects.get(su, su)} session(s) but no registered need"))
    return out


@dataclass
class SolveResult:
    lessons: list[Lesson]
    unscheduled: list[tuple[str, str, int]]  # (student, subject, missing count)
    complete: bool
    nodes_explored: int = 0
    backend: str = "v1"                      # which solver produced this


class _State:
    """Mutable occupancy tracking during search."""

    def __init__(self, data: Dataset, teacher_capacity: int = 2,
                 student_day_cap: int = 2, require_consecutive: bool = True):
        self.data = data
        self.teacher_capacity = teacher_capacity
        self.student_day_cap = student_day_cap
        self.require_consecutive = require_consecutive
        self.teacher_load: Counter = Counter()
        self.teacher_total: Counter = Counter()   # lessons per teacher
        self.student_busy: set[tuple[str, str]] = set()
        self.room_load: Counter = Counter()
        # (student, date) -> set of occupied period numbers
        self.student_day: dict[tuple[str, str], set[int]] = defaultdict(set)

    def fits(self, st: str, su: str, t: str, r: str, s: str) -> bool:
        if self.teacher_load[(t, s)] >= self.teacher_capacity:
            return False
        if (st, s) in self.student_busy:
            return False
        if self.room_load[(r, s)] >= self.data.rooms[r].capacity:
            return False
        slot = self.data.timeslots[s]
        periods = self.student_day[(st, slot.date)]
        if len(periods) >= self.student_day_cap:
            return False                       # H8: daily cap reached
        if self.require_consecutive and periods:
            combined = periods | {slot.period}
            if max(combined) - min(combined) != len(combined) - 1:
                return False                   # H8: must stay contiguous
        return True

    def place(self, l: Lesson) -> None:
        self.teacher_load[(l.teacher_id, l.timeslot_id)] += 1
        self.teacher_total[l.teacher_id] += 1
        self.student_busy.add((l.student_id, l.timeslot_id))
        self.room_load[(l.room_id, l.timeslot_id)] += 1
        slot = self.data.timeslots[l.timeslot_id]
        self.student_day[(l.student_id, slot.date)].add(slot.period)

    def remove(self, l: Lesson) -> None:
        self.teacher_load[(l.teacher_id, l.timeslot_id)] -= 1
        self.teacher_total[l.teacher_id] -= 1
        self.student_busy.discard((l.student_id, l.timeslot_id))
        self.room_load[(l.room_id, l.timeslot_id)] -= 1
        slot = self.data.timeslots[l.timeslot_id]
        self.student_day[(l.student_id, slot.date)].discard(slot.period)


def check_input_problems(data: Dataset) -> list[str]:
    """Detect needs that are impossible before searching (better messages)."""
    problems = []
    for (st, su), need in sorted(data.student_needs.items()):
        if st not in data.students:
            problems.append(f"Need references unknown student '{st}'")
            continue
        if su not in data.subjects:
            problems.append(f"Need references unknown subject '{su}'")
            continue
        teachers = [t for (t, s2) in data.teacher_subjects if s2 == su]
        if not teachers:
            problems.append(
                f"No teacher can teach {data.subjects[su]} "
                f"(needed by {data.students[st]})")
            continue
        slots = {s for s in data.timeslots
                 if (st, s) in data.student_availability
                 and any((t, s) in data.teacher_availability for t in teachers)}
        if len(slots) < need:
            problems.append(
                f"{data.students[st]} needs {need} {data.subjects[su]} "
                f"sessions but only {len(slots)} timeslot(s) work for both "
                f"the student and a capable teacher")
    return problems


def solve(data: Dataset, fixed_lessons: list[Lesson] | None = None,
          teacher_capacity: int = 2,
          student_day_cap: int = 2, require_consecutive: bool = True,
          max_nodes: int = 500_000) -> SolveResult:
    """Greedy-first placement with exhaustive backtracking as fallback.

    v1 is the FAST, approximate solver (v2/CP-SAT is the slow exact one),
    so a cheap greedy pass runs first: it completes almost instantly on
    well-resourced instances — and its least-loaded-teacher / free-day
    ordering tends to give BETTER-balanced schedules than the exhaustive
    search's constrainedness-first order. Only when greedy leaves needs
    unplaced does the full backtracking search with MRV run; it is
    complete within the node budget, so tight instances still get a
    schedule whenever one exists.

    ``fixed_lessons`` are kept as-is and count toward needs; the solver
    schedules only the remainder.  Deterministic: identical input yields an
    identical schedule.  Candidate slots on days where the student has no
    lesson yet are preferred, so students get one lesson per day whenever
    the instance allows it (two consecutive ones only when necessary).
    """
    fixed = list(fixed_lessons or [])
    state = _State(data, teacher_capacity, student_day_cap,
                   require_consecutive)
    for l in fixed:
        state.place(l)

    fixed_count = Counter((l.student_id, l.subject_id) for l in fixed)
    requirements: list[tuple[str, str]] = []
    unschedulable: list[tuple[str, str, int]] = []
    for (st, su), need in sorted(data.student_needs.items()):
        if (st not in data.students or su not in data.subjects):
            unschedulable.append((st, su, need))
            continue
        remaining = need - fixed_count.get((st, su), 0)
        requirements.extend([(st, su)] * max(0, remaining))

    slot_ids = [ts.id for ts in
                sorted(data.timeslots.values(), key=_slot_sort_key)]
    room_ids = sorted(data.rooms, key=lambda r: (-data.rooms[r].capacity, r))
    teachers_for: dict[str, list[str]] = defaultdict(list)
    for (t, su) in sorted(data.teacher_subjects):
        teachers_for[su].append(t)

    def candidates(st: str, su: str,
                   limit: int | None = None) -> list[tuple[str, str, str]]:
        # least-loaded teachers first, so lessons spread evenly from the
        # start instead of piling onto the alphabetically-first teacher.
        # ``limit`` stops the scan early — enough for MRV counting, which
        # only needs to know whether this pair beats the current minimum.
        ranked = sorted(teachers_for.get(su, []),
                        key=lambda t: (state.teacher_total[t], t))
        opts = []
        for i, s in enumerate(slot_ids):
            if (st, s) not in data.student_availability:
                continue
            # prefer days where the student has no lesson yet (soft O1)
            busy_day = bool(state.student_day[(st, data.timeslots[s].date)])
            for t in ranked:
                if (t, s) not in data.teacher_availability:
                    continue
                for r in room_ids:
                    if state.fits(st, su, t, r, s):
                        opts.append((busy_day, i, s, t, r))
                        break  # one room per (slot, teacher) is enough
            if limit is not None and len(opts) >= limit:
                break
        opts.sort(key=lambda x: (x[0], x[1]))
        return [(s, t, r) for (_, _, s, t, r) in opts]

    placed: list[Lesson] = []
    nodes = 0
    exhausted = False

    def greedy_fill() -> Counter:
        """Place every requirement at its first candidate; returns the
        count of requirements that found no spot. Mutates state/placed."""
        missing: Counter = Counter()
        for (st, su) in requirements:
            opts = candidates(st, su)      # full scan: keep the free-day
            if opts:                       # / balance preference ordering
                s, t, r = opts[0]
                l = Lesson(st, su, t, r, s)
                state.place(l)
                placed.append(l)
            else:
                missing[(st, su)] += 1
        return missing

    def unwind() -> None:
        for l in placed:
            state.remove(l)
        placed.clear()

    # ---- fast path: pure greedy (sub-second even on huge terms)
    greedy_missing = greedy_fill()
    if not greedy_missing and not unschedulable:
        return SolveResult(fixed + placed, [], complete=True,
                           nodes_explored=0)
    unwind()

    # the search recurses once per requirement; large terms exceed
    # Python's default 1000-frame limit
    import sys
    _old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(_old_limit, len(requirements) + 500))

    def search(remaining: list[tuple[str, str]]) -> bool:
        nonlocal nodes, exhausted
        if not remaining:
            return True
        if nodes >= max_nodes:
            exhausted = True
            return False
        # MRV: expand the requirement with the fewest candidates right
        # now. Duplicate (student, subject) requirements share the same
        # candidate set, so score each UNIQUE pair once, and count with an
        # early-exit limit — only "fewer than the current best" matters.
        first_index: dict[tuple[str, str], int] = {}
        for i, pair in enumerate(remaining):
            if pair not in first_index:
                first_index[pair] = i
        best: tuple[int, int, tuple[str, str]] | None = None
        for pair, i in first_index.items():
            limit = best[0] if best else None
            opts = candidates(*pair, limit=limit)
            if not opts:
                return False  # dead end — some requirement is unplaceable
            if best is None or len(opts) < best[0]:
                best = (len(opts), i, pair)
                if best[0] == 1:
                    break     # cannot get more constrained than this
        _, idx, (st, su) = best
        # the counting pass may have truncated: branch on the FULL set
        opts = candidates(st, su)
        rest = remaining[:idx] + remaining[idx + 1:]
        for (s, t, r) in opts:
            nodes += 1
            if nodes >= max_nodes:
                exhausted = True
                return False
            l = Lesson(st, su, t, r, s)
            if not state.fits(st, su, t, r, s):
                continue
            state.place(l)
            placed.append(l)
            if search(rest):
                return True
            placed.pop()
            state.remove(l)
        return False

    try:
        complete = search(requirements) and not unschedulable
    finally:
        sys.setrecursionlimit(_old_limit)

    if not complete:
        # The exhaustive search failed too: return the deterministic
        # greedy fill as the best partial schedule, plus an explicit list
        # of what is missing.
        unwind()
        missing = greedy_fill()
        unsched = unschedulable + [
            (st, su, n) for (st, su), n in sorted(missing.items())]
        return SolveResult(fixed + placed, unsched,
                           complete=not unsched, nodes_explored=nodes)

    return SolveResult(fixed + placed, [], complete=True, nodes_explored=nodes)


# ------------------------------------------------- teacher-day optimization

def teacher_day_stats(data: Dataset,
                      lessons: list[Lesson]) -> dict[str, dict]:
    """Per teacher: lesson count and the set of distinct working days."""
    stats = {t: {"lessons": 0, "days": set()} for t in data.teachers}
    for l in lessons:
        if l.teacher_id in stats and l.timeslot_id in data.timeslots:
            stats[l.teacher_id]["lessons"] += 1
            stats[l.teacher_id]["days"].add(data.timeslots[l.timeslot_id].date)
    return stats


def eligible_teachers(data: Dataset) -> list[str]:
    """Teachers who can actually take lessons: at least one teachable
    subject and at least one available timeslot."""
    capable = {t for (t, _su) in data.teacher_subjects}
    available = {t for (t, _s) in data.teacher_availability}
    out = sorted(set(data.teachers) & capable & available)
    return out or sorted(data.teachers)


def student_day_stats(data: Dataset,
                      lessons: list[Lesson]) -> dict[str, dict]:
    """Per student: lesson count, distinct lesson days, and the dates on
    which they have two lessons — the quick check for the 'one lesson per
    day as far as possible' rule."""
    per_day: dict[str, Counter] = {st: Counter() for st in data.students}
    for l in lessons:
        if l.student_id in per_day and l.timeslot_id in data.timeslots:
            per_day[l.student_id][data.timeslots[l.timeslot_id].date] += 1
    return {
        st: {
            "lessons": sum(days.values()),
            "days": len(days),
            "double_days": sorted(d for d, n in days.items() if n >= 2),
        }
        for st, days in per_day.items()
    }


def student_double_days(data: Dataset, lessons: list[Lesson]) -> int:
    """Number of (student, day) pairs with two lessons — the soft O1
    metric ('one lesson per day as far as possible')."""
    per_day: Counter = Counter()
    for l in lessons:
        if l.timeslot_id in data.timeslots:
            per_day[(l.student_id, data.timeslots[l.timeslot_id].date)] += 1
    return sum(1 for n in per_day.values() if n >= 2)


# The soft-objective terms, in the DEFAULT priority order. The user can
# reorder them (UI drag list / `objective_order` on generate) and promote
# any of them to hard caps (`objective_caps` in settings). Nothing here
# is special-cased: "student_day_gap" (multiple lessons on a day must be
# consecutive) is hard by default only because the DEFAULT SETTINGS cap
# it at 0 — demoting it makes it an ordinary soft preference.
OBJECTIVE_TERMS = ("student_double_day", "student_day_gap",
                   "teacher_slot_spread", "teacher_working_day",
                   "teacher_single_day", "teacher_day_spread")

OBJECTIVE_LABELS = {
    "student_double_day": "Student days with two or more lessons",
    "student_day_gap": "Student days with non-consecutive lessons",
    "teacher_slot_spread": "Lesson-count spread between teachers",
    "teacher_working_day": "Total teacher working days",
    "teacher_single_day": "Teacher days with too few lessons",
    "teacher_day_spread": "Working-day spread between teachers",
}


def teacher_single_days(data: Dataset, lessons: list[Lesson],
                        at_most: int = 1) -> int:
    """Number of worked (teacher, day) pairs with AT MOST ``at_most``
    lessons — coming in for so few classes is wasteful for the teacher.
    The threshold is the user-configurable `single_day_max` setting
    (default 1 = days with only one lesson)."""
    per_day: Counter = Counter()
    for l in lessons:
        slot = data.timeslots.get(l.timeslot_id)
        if slot is not None:
            per_day[(l.teacher_id, slot.date)] += 1
    return sum(1 for n in per_day.values() if 1 <= n <= at_most)


def student_gap_days(data: Dataset, lessons: list[Lesson]) -> int:
    """Number of (student, day) pairs whose lessons do NOT form one
    contiguous run of periods."""
    per_day: dict[tuple[str, str], set[int]] = defaultdict(set)
    for l in lessons:
        slot = data.timeslots.get(l.timeslot_id)
        if slot is not None:
            per_day[(l.student_id, slot.date)].add(slot.period)
    return sum(1 for ps in per_day.values()
               if len(ps) >= 2 and max(ps) - min(ps) != len(ps) - 1)


def objective_term_values(data: Dataset,
                          lessons: list[Lesson],
                          single_day_max: int = 1) -> dict[str, int]:
    """Each soft-objective term evaluated on a concrete schedule."""
    stats = teacher_day_stats(data, lessons)
    elig = eligible_teachers(data)
    slot_counts = [stats[t]["lessons"] for t in elig if t in stats]
    day_counts = [len(stats[t]["days"]) for t in elig if t in stats]
    return {
        "student_double_day": student_double_days(data, lessons),
        "student_day_gap": student_gap_days(data, lessons),
        "teacher_slot_spread":
            (max(slot_counts) - min(slot_counts)) if slot_counts else 0,
        "teacher_working_day":
            sum(len(s["days"]) for s in stats.values()),
        "teacher_single_day":
            teacher_single_days(data, lessons, at_most=single_day_max),
        "teacher_day_spread":
            (max(day_counts) - min(day_counts)) if day_counts else 0,
    }


def schedule_objective(data: Dataset, lessons: list[Lesson],
                       order: tuple[str, ...] | list[str] | None = None,
                       single_day_max: int = 1) -> tuple[int, ...]:
    """The soft-objective terms as a tuple, compared lexicographically.

    ``order`` (a permutation of OBJECTIVE_TERMS) decides which term
    dominates; earlier = more important. Default order: one lesson per
    day per student ≻ even teacher lesson counts ≻ few teacher working
    days ≻ even teacher day counts.
    """
    values = objective_term_values(data, lessons,
                                   single_day_max=single_day_max)
    return tuple(values[name] for name in (order or OBJECTIVE_TERMS))


def optimize_teacher_days(data: Dataset, movable: list[Lesson],
                          fixed: list[Lesson] | None = None,
                          teacher_capacity: int = 2,
                          student_day_cap: int = 2,
                          require_consecutive: bool = True,
                          objective_order: list[str] | None = None,
                          max_rounds: int = 200,
                          single_day_max: int = 1) -> list[Lesson]:
    """Deterministic local search improving ``schedule_objective``.

    Only ``movable`` lessons are changed; ``fixed`` ones (e.g. lessons the
    user placed by hand) are kept as-is but count toward all constraints.
    Every accepted move keeps the schedule fully valid, keeps each lesson's
    (student, subject) pair — so coverage is untouched — and strictly
    improves the objective. Move kinds, tried in order:

      * day-block: all of teacher A's movable lessons on one day go to
        teacher B in the same timeslots (packs days / evens teachers out)
      * single lesson: new (teacher, timeslot, room). The target slot must
        be on a day the target teacher already works — packing — UNLESS
        the move shifts a lesson from a busier teacher to one with at
        least two fewer lessons (rebalancing), or moves it off a day where
        its student has two lessons (relieving); those may open a fresh
        working day.

    Each round accepts at most one move; the loop stops at the first round
    with no improvement (or after ``max_rounds``).
    """
    fixed = list(fixed or [])
    work = list(movable)

    # Local search cost grows ~quadratically with schedule size (every
    # candidate move re-validates the whole schedule). Beyond this size
    # the hill climb is skipped — use the exact optimizer (CP-SAT) for
    # quality on large instances; the raw solve is still valid.
    if len(work) > 400:
        return fixed + work

    def ok(candidate: list[Lesson]) -> bool:
        # objective caps are deliberately NOT checked here: the hill climb
        # works TOWARD them (put promoted terms first in objective_order);
        # validate reports any still-unmet cap on the final schedule
        return not validate(data, fixed + candidate, teacher_capacity,
                            student_day_cap, require_consecutive)

    def obj(candidate: list[Lesson]) -> tuple[int, ...]:
        return schedule_objective(data, fixed + candidate, objective_order,
                                  single_day_max=single_day_max)

    if not ok(work):
        return fixed + work   # never touch an already-broken schedule

    lesson_key = (lambda l: (l.student_id, l.subject_id,
                             l.timeslot_id, l.teacher_id, l.room_id))
    teachers_for: dict[str, list[str]] = defaultdict(list)
    for (t, su) in sorted(data.teacher_subjects):
        teachers_for[su].append(t)
    slot_ids = [ts.id for ts in
                sorted(data.timeslots.values(), key=_slot_sort_key)]
    room_ids = sorted(data.rooms)

    for _ in range(max_rounds):
        improved = False
        best = obj(work)
        stats = teacher_day_stats(data, fixed + work)

        # -- day-block reassignment: teacher A's day D -> teacher B
        for a in sorted(data.teachers):
            for day in sorted(stats[a]["days"]):
                block = [i for i, l in enumerate(work)
                         if l.teacher_id == a
                         and data.timeslots[l.timeslot_id].date == day]
                if not block:
                    continue   # that day is only fixed lessons
                for b in sorted(data.teachers):
                    if b == a:
                        continue
                    cand = list(work)
                    for i in block:
                        cand[i] = Lesson(work[i].student_id,
                                         work[i].subject_id, b,
                                         work[i].room_id,
                                         work[i].timeslot_id, id=work[i].id)
                    if obj(cand) < best and ok(cand):
                        work, best, improved = cand, obj(cand), True
                        break
                if improved:
                    break
            if improved:
                break
        if improved:
            continue

        # -- single-lesson moves
        double_days = {key for key, n in Counter(
            (x.student_id, data.timeslots[x.timeslot_id].date)
            for x in fixed + work).items() if n >= 2}
        for i in sorted(range(len(work)), key=lambda i: lesson_key(work[i])):
            l = work[i]
            relieve = ((l.student_id, data.timeslots[l.timeslot_id].date)
                       in double_days)
            days_wo = {data.timeslots[x.timeslot_id].date
                       for j, x in enumerate(fixed + work)
                       if j != len(fixed) + i and
                       x.teacher_id == l.teacher_id}
            for t in teachers_for.get(l.subject_id, []):
                if t == l.teacher_id:
                    t_days = days_wo
                    rebalance = False
                else:
                    t_days = stats[t]["days"]
                    rebalance = (stats[l.teacher_id]["lessons"]
                                 - stats[t]["lessons"] >= 2)
                for s in slot_ids:
                    if (s != l.timeslot_id and not rebalance and not relieve
                            and data.timeslots[s].date not in t_days):
                        continue   # fresh day, no rebalance/relief: skip
                    # cheap pre-filters before the full validate
                    if (t, s) not in data.teacher_availability:
                        continue
                    if (l.student_id, s) not in data.student_availability:
                        continue
                    for r in room_ids:
                        if (t, s, r) == (l.teacher_id, l.timeslot_id,
                                         l.room_id):
                            continue
                        cand = list(work)
                        cand[i] = Lesson(l.student_id, l.subject_id,
                                         t, r, s, id=l.id)
                        if obj(cand) < best and ok(cand):
                            work, best, improved = cand, obj(cand), True
                            break
                    if improved:
                        break
                if improved:
                    break
            if improved:
                break
        if not improved:
            break

    return fixed + work
