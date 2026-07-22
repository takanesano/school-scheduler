# Cram School Scheduler — notes for Claude

Locally-run FastAPI + SQLite + vanilla-JS web app that schedules
cram-school lessons over a term of concrete calendar dates — a "summer
school" model where every day is unique (students × subject needs ×
teachers × rooms × timeslots). See README.md for user-facing docs.

Date model: `timeslots.date` is an ISO `YYYY-MM-DD` string (validated at
CSV import and API level; `csv_io._is_iso_date`). Slot ordering is plain
string sort. Needs are TOTAL sessions over the term (`sessions` column,
not per-week). The H8 "spread" constraint means max one session of a
subject per calendar date per student. All UI views render as month-style
calendars: week rows Mon–Sun built by `views._grid`, which pads to whole
weeks and marks non-term days `in_term: false`.

## Commands

```bash
.venv/bin/python -m pytest              # run all tests (must stay green)
.venv/bin/uvicorn app.main:app          # run the server (http://127.0.0.1:8000)
.venv/bin/python -m app.load_sample sample_data   # seed the DB with demo data
```

## Architecture rules

- **`app/scheduler.py` is pure** — no I/O, no DB, no FastAPI imports.
  All correctness logic (constraints H1–H8, coverage, solver) lives here so
  it can be unit-tested exhaustively. Keep it that way.
- The **validator (`validate`) is the source of truth** for what a legal
  schedule is. The solver must never place a lesson the validator would
  reject; several tests assert `validate(...) == []` on solver output. If
  you add a constraint, add it to BOTH `validate` and `_State.fits`, plus
  tests for each side.
- The solver is deterministic (sorted iteration everywhere). Tests rely on
  this; don't introduce hash-order or randomness.
- v1's IDENTITY is "fast, approximate": solve() is GREEDY-FIRST
  (nodes_explored == 0 on the fast path) and only falls back to the
  exhaustive MRV backtracking search when greedy leaves needs unplaced —
  never make v1 slow again in the name of quality; quality is v2's job
  (CP-SAT). The greedy ordering (least-loaded teacher, free-day-first)
  also happens to balance better than the search's constrainedness
  order. MRV itself scores unique (student, subject) pairs with
  early-exit counting; the recursion limit is raised per instance;
  optimize_teacher_days skips >400 movable lessons.
- H5 is teacher CAPACITY (default 2): a teacher may teach two students at
  once, different subjects allowed — code `teacher_over_capacity`, not
  double-booked. `teacher_capacity` is a parameter on validate/solve.
- H8 is the STUDENT DAY rule (always on; the old optional per-subject
  spread flag `one_subject_session_per_day` is GONE from every signature
  and the API): max two lessons per student per calendar day
  (`student_day_overload`), and two must be consecutive period numbers
  (`student_day_gap`; same-slot pairs are H6's business, not H8's).
  Soft top objective: minimize `student_double_days` (one lesson/day as
  far as possible) — first element of `schedule_objective`, which is now
  (student_double_days, student_day_gaps, slot_spread, total_days,
  teacher_single_days, day_spread).
  `student_day_stats` powers the Status-panel per-student monitor
  (`student_stats` in GET /api/schedule; rows with double days get the
  `double-day` warning highlight).
- Soft-objective priority is USER-SORTABLE: `OBJECTIVE_TERMS` names the
  terms (currently six, incl. `teacher_single_day` = worked days where
  a teacher has at most `single_day_max` lessons — that threshold is a
  persisted setting, default 1, edited inline on the rule card and
  threaded as a `single_day_max=` kwarg through
  validate/objective_term_values/schedule_objective/
  optimize_teacher_days/SolverConfig; CP encoding
  `(K+1)*wd - load <= K*sd`); `schedule_objective(data, lessons,
  order)` and
  `ObjectiveWeights.lexicographic(order)` both take a permutation, and
  generate accepts `objective_order` (422 on non-permutations). The UI's
  drag list (`state.objOrder`, `#prio-list`) is the source of that order.
- Hard-constraint SETTINGS persist in the `settings` table (GET/PUT
  /api/settings): teacher_capacity, student_day_cap, single_day_max,
  and `objective_caps` (an objective at "priority 0" — dragged above the
  divider in the single rules list — becomes a hard "term ≤ bound" cap;
  the scalar settings currently have no UI, API only). Consecutiveness
  is NOT a boolean setting anymore: it is the `student_day_gap`
  objective term, hard by default via objective_caps {student_day_gap:
  0} in DEFAULT_SETTINGS; `require_consecutive` survives only as an
  internal solver/validate parameter derived via `_hard_consecutive`.
  Legacy DBs with a stored require_consecutive row are folded into
  objective_caps once at startup (`_migrate_settings`; the legacy row is
  the migration marker — never re-add it). EVERY validate call in
  main.py goes through
  `_validate_with_settings`; H8 is generalized (configurable day cap;
  contiguous-run consecutiveness for any cap). Cap semantics: CP-SAT
  enforces caps as model constraints; v1 cannot — it front-loads capped
  terms in its hill-climb order and `validate` reports
  `objective_cap_exceeded` (no lesson ids, so manual adds are not
  blocked by caps). solve_v2's cost gate only prefers v1 when v1 itself
  satisfies the caps.
- `optimize_teacher_days` is a post-solve local search (day-block handover
  and single-lesson moves) improving `schedule_objective` =
  (lesson-count spread, total teacher working days, day-count spread) over
  ELIGIBLE teachers (≥1 subject and ≥1 availability), lexicographic —
  balancing lesson counts beats packing days beats evening day counts.
  Rebalance moves (busier→2+-lessons-lighter teacher) may open a fresh
  working day; packing moves may not. The solver also assigns
  least-loaded-teacher-first so schedules start roughly balanced.
  It never touches
  `fixed` lessons (user-placed, i.e. id not None at generate time), never
  changes (student, subject) pairs, and refuses to run on an
  already-invalid schedule. GET /api/schedule returns `teacher_stats` and
  `objective` for the Status-panel workload table.
- `app/main.py` route order matters: the generic `GET /api/{entity}` route
  is registered LAST so fixed paths like `/api/schedule` match first.
- CSV import is all-or-nothing per file and uses upsert-then-prune for
  id-keyed tables so re-importing a parent CSV does not cascade-delete
  children (see `test_reimport_same_parent_keeps_children`).
- SQLite connections are per-request with `check_same_thread=False`
  (async endpoints run on a different thread than the sync dependency).
- Calendar output views (overview / per-student / per-teacher) are built
  by pure functions in `app/views.py` sharing one weeks-grid JSON shape,
  served under `/api/views/...`, rendered by the Calendars tab (and the
  Schedule tab's timetable, via overview + `lesson_id`) with print CSS.
  New `/api/...` fixed paths must be registered before `GET /api/{entity}`.
- Sample data is generated (two-week summer term 2026-07-27…2026-08-08);
  slot ids look like `0727-1` (= MMDD-period). `POST /api/timeslots/bulk`
  mass-creates slots for a date range × weekdays × periods; it skips
  existing (date, period) pairs and falls back to YYYYMMDD-period ids on
  MMDD collisions (range capped at 400 days).
- Lesson moves (drag-and-drop) and inline edits (✎ button: subject /
  teacher / room dropdowns in the card) both go through
  `PATCH /api/lessons/{id}` via the shared `patchLesson` caution flow,
  which validates like POST: 409 + violations unless `force`; unknown
  references are a hard 422 (FK would fail). Dismissing the confirm
  leaves the inline editor open on purpose.
- `render()` restores `window.scrollY` when re-rendering the SAME tab
  (drag/edit/toggle must not jump to the top); a tab switch scrolls to 0.
  Any future partial-update refactor must keep this behavior.
- NEVER use window.confirm/alert — browsers offer "prevent additional
  dialogs" which silently auto-cancels later dialogs and breaks the
  caution flow. Use `appConfirm(message, okLabel)` (in-page modal,
  returns `Promise<boolean>`; Esc/backdrop = cancel, Enter = OK). The editor shows live ✓/✗
  feedback via `POST /api/lessons/{id}/check_options` — a dry-run that
  substitutes each option into the proposal and runs the real validator
  (never duplicate constraint logic in JS). An option's ✗ means "the whole
  combination would be invalid with this choice", not "this field is the
  culprit"; the per-option messages are in the option tooltips.
  `calendarTable(data, entryHtml, slotHook)` — the Schedule tab's
  `slotHook` wires dragover/drop per slot block; cards carry
  `data-lesson-id`, slot blocks `data-slot-id`. Headless-shell Chromium
  can't native-drag; UI smoke tests dispatch synthetic DragEvents.
- Frontend is a single no-build page (`app/static/app.js`). The
  spread/keep options persist in the JS `state` object; `GET /api/schedule`
  must be fetched with the same `one_subject_session_per_day` value the
  schedule was generated with, or H8 false-positives appear.

- `app/solver_v2.py` is the IMPLEMENTED weight-driven CP-SAT solver
  (docs/solver-v2-plan.md). Key invariants: `solve_v2` always runs the
  v1 pipeline too and only returns the CP answer when it passes
  validate+coverage AND has weighted_cost ≤ v1's; determinism comes from
  `max_deterministic_time` (never rely on wall-clock cutoffs); the v1
  solution (or reference) is fed as a COMPLETE hint — hint every
  variable including zeros, partial hints get dropped by CP-SAT.
  `resolve_minimal_disruption` = reschedule with a dominating
  changed-lesson weight. Opt-in via `solver: "v2"` on generate / the
  "exact optimizer" checkbox; ortools is an optional dependency (module
  degrades to v1 without it).

## Conventions

- Line length 100, E741 ignored (`l` = lesson) — see setup.cfg.
- Entity ids are user-chosen strings (e.g. `s1`, `mon-2`), not autoincrement;
  only `lessons.id` is an integer autoincrement.
- Any bug fix gets a regression test first.
