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
(`school.db`) and every change is saved to it immediately — there is
no separate "save" step. Back up or move to another PC with the
Database backup panel (CSV tab), or by copying that one file. No
internet connection or external service is needed.

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
4. Lessons in a room never exceed its capacity, and — when the room has
   a **teacher limit** — no more than that many different teachers are
   in the room at the same time (0 = no limit).
5. A student has at most **two lessons per calendar day**, and when there
   are two they must be in **consecutive periods** (no gap in between).
6. A teacher never exceeds their own **max lessons per day**, when one
   is set on the Teachers tab (0 = no limit).
7. A student assigned to a teacher with **priority 0** on the
   Assignments tab is taught by that teacher **only**.

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
top are the built-in hard constraints. Below them, the seven conditions —
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

Both solvers can be **cancelled mid-run** (a Cancel button appears
next to the progress bar); a cancelled generation leaves the stored
schedule untouched. The drag-sorted priority order is a **saved
setting** — it survives reloads and is used by every generate.

Two solvers, one explicit trade-off (chosen in the Generate panel):

- **Standard** — fast and approximate, by design. A greedy pass places
  everything in well under a second on typical data; only when it gets
  stuck does a complete backtracking search take over, so a full
  schedule is still found whenever one exists (within a node budget).
  If the inputs make a complete schedule impossible, you get the best
  partial schedule plus exactly which needs could not be placed and why.
  A **failsafe wall-clock limit** guarantees it can never run away: if
  the backtracking search hasn't finished after ~20 seconds it is cut
  off and the greedy best-effort is returned instead (a warning toast
  says so). The same limit bounds the standard solver when it runs as
  the exact solver's warm start, so warm-up never eats the search
  budget.
- **Exact (CP-SAT)** — models the whole problem as a constraint program
  with OR-tools and optimizes every priority at once, using all CPU
  cores. It keeps searching for its whole **search budget** (real
  seconds, configurable), so generation takes roughly that long — but
  the result is usually much better: on the large sample term two
  minutes of budget roughly halves the teacher working days and
  eliminates single-lesson days entirely. The schedule that existed
  when you clicked Generate is the bar to beat: if the optimizer finds
  nothing better within its budget, your existing schedule (hand-tuned
  or from an earlier run) is kept unchanged rather than replaced (its
  output is always re-checked by the same validator). Requires the
  optional `ortools` dependency.

## How assignment priorities work

The Assignments tab pairs students with the teachers in charge of them.
A pair's priority number picks one of two very different regimes:

- **Priority 0 — a hard rule.** The student may **only** be taught by
  their priority-0 teacher(s). The validator rejects any other teacher,
  manual edits go through the usual confirm-to-override flow, and both
  solvers never even consider anyone else.
- **Priorities 1–9 — a soft preference**, scored in penalty points. A
  student with at least one assignment (any priority) is a *paired*
  student. Every lesson of theirs taught by a teacher **outside their
  assigned set** costs `10 − (the strongest priority in their row)`
  points — ignoring a priority-1 assignment costs 9 points per lesson,
  a priority-9 one just 1 point. Lessons with any assigned teacher are
  free. Nothing is forbidden; the schedule is simply "worse" by that
  many points, and both solvers minimize the total as the draggable
  **"Students taught by their assigned teacher"** condition — its
  position in the rules list decides what it may be traded against,
  and dragging it above the divider turns it into a hard cap
  (bound 0 = every paired student stays with their assigned teachers,
  or generation reports a violation).

Blank cells have no meaning of their own — it depends on the row:

- A student whose **whole row is blank** is neutral: no penalty ever,
  any qualified teacher is equally fine.
- Once a row has **any** assignment, every blank cell in it means
  "not one of this student's teachers" — a lesson with that teacher
  costs the points above. The assigned set is a whitelist for the
  scoring: inside = free, outside = costs points. To make several
  teachers acceptable, assign them all — their priorities may differ,
  and the strongest one sets the price of going outside the set.

Under the hood, the standard solver tries a student's assigned teachers
first (strongest priority first) when placing each lesson; the exact
solver prices the points directly in its objective.

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
  **edit its subject, teacher, or room in place**. Add a lesson right
  where it belongs: the **＋ button on any timeslot** (or a
  **right-click** on the slot) opens a mini form pre-targeted at that
  slot — same validation and confirm-to-override flow as the add
  panel at the top. The 🔓/🔒 button
  **locks a lesson in place**: locked lessons are always pinned when
  generating (both solvers schedule around them), survive "Clear
  schedule", and refuse drags, edits and deletion until unlocked.
  **Select lessons…** turns on selection mode: click cards to select
  several lessons, or **drag a rectangle over the timetable** to add
  every lesson it touches (the page scrolls itself at the viewport
  edge; the selection survives leaving the mode) — or hit **Select
  all** (respects the filter). **🔒 Lock selected / 🔓 Unlock
  selected** flips the lock on the whole selection at once. Then
  **repeat them over the next N weeks** — each copy lands on the same
  weekday and period with the same student, subject, teacher and room
  — or **change the subject / teacher / room of all selected lessons
  at once** ("change selected to:", pick only the fields to change).
  Weeks without a matching timeslot, existing copies, and locked
  lessons are skipped (the result message says how many); conflicting
  changes go through the usual confirm-to-override flow. **↩ Undo**
  walks back the last manual edits one at a time (add, move, edit,
  bulk change, repeat, delete — up to 20 steps); generating or
  clearing the schedule resets the undo history, so undo never rolls
  back a solver result. While choosing, every
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
- **Student needs** tab — an editable student × subject matrix: type
  the session count straight into a cell (saved immediately; blank or
  0 removes the need), with a per-student total column — plus which
  subjects each teacher can teach.
- **Assignments** tab — who is in charge of whom: click a cell in the
  student × teacher grid to open a picker for the pair's rigidity.
  **0** (red) is a hard rule, **1-3** (blue; up to 9 via API/CSV) a
  soft preference, blank = not assigned — the exact semantics are in
  [How assignment priorities work](#how-assignment-priorities-work)
  above. Here too, **drag to select a block** and set every
  cell to one value, clear them, or copy/paste the block — with the
  same ↩ Undo support.
- **Availability** tab — click cells in the per-date grid to toggle
  teacher and student availability. **Drag across cells to select a
  rectangle** (the grid auto-scrolls when the cursor reaches its
  edge), or click/drag along the **name column or the period/date
  headers to select whole rows and columns** — a date header selects
  all of that day's periods. Then use the action bar to make the
  whole block
  available / unavailable / inverted, or **copy** it and **paste** it
  elsewhere (select the target's top-left cell — a click with a tiny
  drag selects a single cell — and paste; teacher ↔ student grids are
  interchangeable). Bulk changes are applied in one transaction, and
  the tab's own **↩ Undo** button walks back grid edits (cell toggles
  and block edits alike) — it shares one history with the timetable's
  undo.
- **CSV import/export** tab — upload or download any table as CSV.
  The **Database backup** panel below it downloads the whole database
  as one timestamped `.db` file (master data, the schedule with its
  locks, assignments, settings — a consistent snapshot, safe while
  the app is running) and restores from such a file, replacing
  everything after a confirmation. Older backups are migrated to the
  current schema automatically on restore.

## CSV formats

Import base tables before link tables (the order below works). Import is
all-or-nothing per file: if any row is invalid, nothing changes and every
error is reported with its line number. Importing a file makes it the
table's full contents — rows absent from the file are deleted (deleting a
student also removes their availability, needs, and lessons).

| File | Header |
| --- | --- |
| `students.csv` | `id,name` |
| `teachers.csv` | `id,name,max_lessons_per_day` (max_lessons_per_day optional: daily lesson cap, 0 = no limit) |
| `subjects.csv` | `id,name` |
| `rooms.csv` | `id,name,capacity,teacher_capacity` (teacher_capacity optional: max distinct teachers per timeslot, 0 = no limit) |
| `timeslots.csv` | `id,date,period,label` (date: `YYYY-MM-DD`; label optional, e.g. `09:00-10:10`) |
| `teacher_subjects.csv` | `teacher_id,subject_id` |
| `teacher_students.csv` | `teacher_id,student_id,priority` (priority optional: 0 = must, 1-9 = soft preference, default 1) |
| `student_needs.csv` | `student_id,subject_id,sessions` (total over the term) |
| `teacher_availability.csv` | `teacher_id,timeslot_id` |
| `student_availability.csv` | `student_id,timeslot_id` |

See [sample_data/](sample_data/) for a complete working example — a
two-week summer term (2026-07-27 … 2026-08-08, no Sundays).

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
