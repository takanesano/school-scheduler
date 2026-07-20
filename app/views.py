"""Calendar view builders — pure functions from (Dataset, lessons) to
JSON-ready month-calendar structures.

Timeslots live on real calendar dates (a "summer school" term), so all
three views share one calendar shape: rows of whole weeks (Mon–Sun) from
the week containing the earliest timeslot to the week containing the
latest.

    {
      "periods": [1, 2, 3],                    # union over the term
      "weeks": [                               # each week = exactly 7 cells
        [ {"date": "2026-07-27", "weekday": "Mon",
           "in_term": true,                    # false: date has no timeslots
           "slots": [ {"period": 1, "timeslot_id": "0727-1",
                       "label": "17:00-18:10", "entries": [...]}, ... ]},
          ... ], ...
      ]
    }

Entry shapes per view:
  overview       {teacher_id, teacher_name,
                  lessons: [{lesson_id, student_id, student_name, subject_id,
                             subject_name, room_id, room_name}]}
  student        {subject_id, subject_name, teacher_id, teacher_name,
                  room_id, room_name}
  teacher        {subject_id, subject_name, student_id, student_name,
                  room_id, room_name}

A valid schedule has at most one entry per slot in the student and teacher
views (constraints H5/H6); lists are used so force-saved conflicting
schedules still render everything.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .scheduler import Dataset, Lesson, _slot_sort_key

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _grid(data: Dataset) -> tuple[list[int], list[list[dict]], dict[str, dict]]:
    """Build the week rows; returns (periods, weeks, slot-cell index)."""
    slots = sorted(data.timeslots.values(), key=_slot_sort_key)
    periods = sorted({s.period for s in slots})
    if not slots:
        return periods, [], {}
    by_date = defaultdict(list)
    for s in slots:
        by_date[s.date].append(s)
    first = dt.date.fromisoformat(min(by_date))
    last = dt.date.fromisoformat(max(by_date))
    start = first - dt.timedelta(days=first.weekday())      # back to Monday
    end = last + dt.timedelta(days=6 - last.weekday())      # forward to Sunday

    weeks: list[list[dict]] = []
    index: dict[str, dict] = {}
    day = start
    while day <= end:
        week = []
        for _ in range(7):
            iso = day.isoformat()
            cell = {
                "date": iso,
                "weekday": WEEKDAYS[day.weekday()],
                "in_term": iso in by_date,
                "slots": [{"period": s.period, "timeslot_id": s.id,
                           "label": s.label, "entries": []}
                          for s in by_date.get(iso, [])],
            }
            for slot_cell in cell["slots"]:
                index[slot_cell["timeslot_id"]] = slot_cell
            week.append(cell)
            day += dt.timedelta(days=1)
        weeks.append(week)
    return periods, weeks, index


def _names(data: Dataset, l: Lesson) -> dict:
    return {
        "lesson_id": l.id,
        "student_id": l.student_id,
        "student_name": data.students.get(l.student_id, l.student_id),
        "subject_id": l.subject_id,
        "subject_name": data.subjects.get(l.subject_id, l.subject_id),
        "teacher_id": l.teacher_id,
        "teacher_name": data.teachers.get(l.teacher_id, l.teacher_id),
        "room_id": l.room_id,
        "room_name": (data.rooms[l.room_id].name
                      if l.room_id in data.rooms else l.room_id),
    }


def build_overview(data: Dataset, lessons: list[Lesson]) -> dict:
    """All lessons per timeslot, grouped by teacher."""
    periods, weeks, index = _grid(data)
    by_slot_teacher: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for l in lessons:
        if l.timeslot_id in index:
            by_slot_teacher[l.timeslot_id][l.teacher_id].append(l)
    for slot_id, by_teacher in by_slot_teacher.items():
        entries = []
        for teacher_id in sorted(
                by_teacher, key=lambda t: data.teachers.get(t, t)):
            group = sorted(by_teacher[teacher_id],
                           key=lambda l: (data.students.get(l.student_id,
                                                            l.student_id),
                                          l.subject_id))
            entries.append({
                "teacher_id": teacher_id,
                "teacher_name": data.teachers.get(teacher_id, teacher_id),
                "lessons": [
                    {k: v for k, v in _names(data, l).items()
                     if not k.startswith("teacher")}
                    for l in group],
            })
        index[slot_id]["entries"] = entries
    return {"periods": periods, "weeks": weeks}


def build_student_view(data: Dataset, lessons: list[Lesson],
                       student_id: str) -> dict:
    """One student's term: subject (plus teacher and room) per timeslot."""
    if student_id not in data.students:
        raise KeyError(student_id)
    periods, weeks, index = _grid(data)
    for l in sorted(lessons, key=lambda l: l.subject_id):
        if l.student_id == student_id and l.timeslot_id in index:
            entry = _names(data, l)
            entry.pop("student_id"), entry.pop("student_name")
            index[l.timeslot_id]["entries"].append(entry)
    return {"student_id": student_id,
            "student_name": data.students[student_id],
            "periods": periods, "weeks": weeks}


def build_teacher_view(data: Dataset, lessons: list[Lesson],
                       teacher_id: str) -> dict:
    """One teacher's term: subject and student per timeslot."""
    if teacher_id not in data.teachers:
        raise KeyError(teacher_id)
    periods, weeks, index = _grid(data)
    for l in sorted(lessons,
                    key=lambda l: (data.students.get(l.student_id,
                                                     l.student_id),
                                   l.subject_id)):
        if l.teacher_id == teacher_id and l.timeslot_id in index:
            entry = _names(data, l)
            entry.pop("teacher_id"), entry.pop("teacher_name")
            index[l.timeslot_id]["entries"].append(entry)
    return {"teacher_id": teacher_id,
            "teacher_name": data.teachers[teacher_id],
            "periods": periods, "weeks": weeks}
