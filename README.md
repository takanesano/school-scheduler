# Cram School Scheduler

> 🇯🇵 **Windows で使いたい方(初心者向け)**:
> [かんたんインストールガイド (INSTALL.ja.md)](INSTALL.ja.md) を
> ご覧ください。コマンド入力なし・ダブルクリックだけで動かせます。

A locally-run web application for building and managing the lesson schedule
of a cram school term — e.g. a summer school where every calendar day is
unique. It tracks students and the subjects they need, teachers and the
subjects they can teach, classrooms, and everyone's per-date availability —
then generates a conflict-free schedule automatically, or lets you place
lessons by hand with instant validation. All views are month-style
calendars (week rows, Mon–Sun columns).

Everything runs on your machine; data lives in a single SQLite file
(`school.db`). No internet connection or external service is needed.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/uvicorn app.main:app
```

Then open <http://127.0.0.1:8000> in a browser.

To try it with the bundled example data first:

```bash
.venv/bin/python -m app.load_sample sample_data
```

## Concepts

| Term | Meaning |
| --- | --- |
| Timeslot | One teaching period on one concrete date, e.g. `2026-07-27` period 2 (10:20–11:30) |
| Need | "Student X needs N sessions of subject Y over the term" |
| Lesson | One scheduled session: student + subject + teacher + room + timeslot |
| Room capacity | How many simultaneous lessons fit in the room (booths) |

A generated schedule always satisfies these hard constraints:

1. The teacher can teach the lesson's subject.
2. Teacher and student are both available at the timeslot.
3. A student has at most one lesson per timeslot; a teacher has at most
   **two** — a teacher may teach two students at once, even in different
   subjects.
4. Lessons in a room never exceed its capacity.
5. A student has at most **two lessons per calendar day**, and when there
   are two they must be in **consecutive periods** (no gap in between).

After solving, an optimization pass (on by default, toggleable) improves
the soft objectives without changing who learns what, in strict priority
order: (1) students get **one lesson per day as far as possible** —
two-lesson days only when unavoidable; (2) lesson counts are as even as
possible across teachers — no idle teacher next to an overloaded one;
(3) each teacher's lessons are packed into as few working days as
possible; (4) **days where a teacher has too few lessons are kept to a
minimum** — the threshold is editable right on the rule card ("few
teacher days with at most N lessons", default 1); (5) working-day
counts are evened out. The Status panel shows a
per-teacher lessons / working-days table and a per-student table flagging
every two-lesson day with its date, plus all four metrics — so each
objective can be checked at a glance.

The Generate panel shows all rules as **one list**. Locked cards at the
top are the built-in hard constraints. Below them, the six conditions —
one lesson per day per student, multiple-lessons-must-be-consecutive,
and the four teacher-workload objectives (including "few teacher days
with at most N lessons", where N is edited on the card) — are
draggable cards whose
order is the lexicographic priority both solvers optimize
(1 = most important). The consecutiveness condition starts at priority 0
(always active) by default. Dragging a card **above the
divider gives it priority 0 = always active**: it becomes a hard
constraint with an editable bound (e.g. "lesson-count spread ≤ 1") — the
exact optimizer enforces the bound in its model; the standard solver
works toward it first and the Status panel reports a violation if it
cannot be met. Drag the card back below the divider to make it a soft
priority again.

Two solvers, one explicit trade-off (chosen in the Generate panel):

- **Standard** — fast and approximate, by design. A greedy pass places
  everything in well under a second on typical data; only when it gets
  stuck does a complete backtracking search take over, so a full
  schedule is still found whenever one exists (within a node budget).
  If the inputs make a complete schedule impossible, you get the best
  partial schedule plus exactly which needs could not be placed and why.
- **Exact (CP-SAT)** — models the whole problem as a constraint program
  with OR-tools and optimizes every priority at once. It keeps searching
  for its whole **search budget** (configurable, default ~8 s), so
  generation takes roughly that long — but the result is usually
  strictly better; on the sample term it needs several fewer teacher
  working days. It automatically falls back to the standard solver when
  it cannot do better (its output is always re-checked by the same
  validator). Requires the optional `ortools` dependency.

## Using the web interface

- **Calendars** tab — three printable month-style calendar views: an
  **overview** of every timeslot grouped by teacher with their students, a
  **per-student** calendar showing that student's subject in each timeslot,
  and a **per-teacher** calendar showing the subject and student they teach
  in each timeslot. The Print button produces a clean handout (controls
  and navigation are hidden when printing).
- **Schedule** tab — generate/clear the timetable, see violations and
  coverage warnings, add or delete individual lessons, **drag a lesson
  card onto another timeslot to move it**, and use the card's ✎ button to
  **edit its subject, teacher, or room in place**. While choosing, every
  dropdown option is marked ✓/✗ live (would the lesson be valid with that
  choice?), and the current combination's would-be violations are shown
  before you save. Manual additions and
  moves that break a constraint are rejected with an explanation (you can
  override after a confirmation; overridden conflicts are highlighted red).
  A toggle in the Status panel controls whether that confirmation is asked
  at all — with it off, conflicting changes save immediately. Violations
  are always listed in the Status panel either way.
- **Students / Teachers / Subjects / Rooms / Timeslots** tabs — add,
  rename, delete master data. The Teachers tab also shows each teacher's
  teachable subjects and a clickable teacher × subject matrix to edit them.
  The Timeslots tab has a **mass-add** form: pick a date range, weekdays,
  and periods (with time labels) to create a whole term's slots at once;
  existing (date, period) pairs are skipped, never overwritten.
- **Student needs** tab — set total sessions per student and subject,
  and which subjects each teacher can teach.
- **Availability** tab — click cells in the per-date grid to toggle
  teacher and student availability.
- **CSV import/export** tab — upload or download any table as CSV.

## CSV formats

Import base tables before link tables (the order below works). Import is
all-or-nothing per file: if any row is invalid, nothing changes and every
error is reported with its line number. Importing a file makes it the
table's full contents — rows absent from the file are deleted (deleting a
student also removes their availability, needs, and lessons).

| File | Header |
| --- | --- |
| `students.csv` | `id,name` |
| `teachers.csv` | `id,name` |
| `subjects.csv` | `id,name` |
| `rooms.csv` | `id,name,capacity` |
| `timeslots.csv` | `id,date,period,label` (date: `YYYY-MM-DD`; label optional, e.g. `09:00-10:10`) |
| `teacher_subjects.csv` | `teacher_id,subject_id` |
| `student_needs.csv` | `student_id,subject_id,sessions` (total over the term) |
| `teacher_availability.csv` | `teacher_id,timeslot_id` |
| `student_availability.csv` | `student_id,timeslot_id` |

See [sample_data/](sample_data/) for a complete working example — a
deliberately large stress-test term: **60 students, 10 teachers,
2026-07-21 … 2026-08-31** (Sundays off) with 6 periods per day, a single
12-seat hall, and ~3 subjects per student with exactly 5 sessions each
(~900 lessons).
Every teacher covers Japanese / English / Social Studies; only five also
teach Math and Science. The standard solver generates it in about a
second. Regenerate or
customize it with [scripts/generate_sample_data.py](scripts/generate_sample_data.py)
(deterministic; edit the constants at the top).

## Tests

```bash
.venv/bin/python -m pytest
```

The suite (98 tests) covers every hard constraint of the validator, solver
completeness/backtracking/partial-schedule behavior, CSV parsing edge cases
and atomicity, and the REST API end to end.

## Project layout

- [app/scheduler.py](app/scheduler.py) — pure domain logic: validator, solver, diagnostics
- [app/csv_io.py](app/csv_io.py) — CSV parse/import/export
- [app/db.py](app/db.py) — SQLite schema and connections
- [app/main.py](app/main.py) — FastAPI REST API + static file serving
- [app/static/](app/static/) — single-page web UI (no build step)
- [tests/](tests/) — pytest suite
