# Solver v2 — plan for a more tailored scheduling algorithm

Status: **Phases 1–3 implemented** in [app/solver_v2.py](../app/solver_v2.py)
(CP-SAT via OR-tools; weighted cost function; minimal-disruption
`resolve_minimal_disruption`). Exposed in the API/UI as an opt-in
(`solver: "v2"` / the "exact optimizer (CP-SAT)" checkbox); the v1
pipeline remains the default. Phase 4 (native fallback improvements) is
not planned unless the ortools dependency becomes a problem.

Implementation notes beyond the original plan:

- Determinism uses CP-SAT's `max_deterministic_time` (reproducible work
  units) as the primary cutoff, with `max_time_in_seconds` only as a
  wall-clock safety net — a plain wall-clock limit made timeout
  incumbents unreproducible.
- The v1 solution (or the reference schedule when rescheduling) is fed
  in as a COMPLETE solution hint — every variable hinted, zeros included.
  Partial hints (positives only) get abandoned by CP-SAT's hint-repair
  and were effectively ignored.
- Measured on sample_data: v2 reaches objective terms
  (double-days 0, slot spread 1, teacher-days 14, day spread 1) vs v1's
  (0, 1, 18, 1) — four fewer teacher working days.

## Why v2

The v1 pipeline is *feasibility-first*: backtracking finds any complete
schedule, then a greedy local search improves the objectives. That works,
but has structural limits:

1. **Local optima.** Single-move hill climbing cannot cross valleys — e.g.
   swapping two lessons between teachers is invisible if each half alone
   makes things worse. Seen in practice: teacher lesson spread stuck at 3
   when the true optimum given subject coverage may be lower.
2. **Fixed lexicographic priorities.** (student one-per-day) ≻ (teacher
   balance) ≻ (day packing) ≻ (day balance) is hard-coded. Real schools
   weigh these differently, and some want trade-offs ("2 extra teacher
   days is worth 1 fewer two-lesson day"), which a lexicographic order
   cannot express.
3. **No optimality signal.** We never know how far from optimal we are.
4. **Rescheduling is destructive.** Regenerating mid-term rebuilds
   everything; there is no "change as little as possible" mode after a
   teacher calls in sick.

## Direction

Model the whole problem as a single weighted optimization and solve it
exactly (small instances) or near-exactly with a principled search:

### Phase 1 — objective/constraint registry (pure refactor)

- Introduce `SolverConfig` (see skeleton): hard-constraint toggles/values
  (teacher capacity, student day cap, consecutiveness) and per-objective
  weights, with `lexicographic()` presets reproducing v1 exactly.
- Rewrite `schedule_objective` as a weighted sum over named terms so v1
  optimizer and v2 share one cost function. v1 behavior = preset weights
  with dominating magnitudes. Regression-test equality against v1 tuples.

### Phase 2 — CP-SAT model (preferred backend)

- Optional dependency `ortools`; module degrades gracefully when absent.
- Variables: one boolean per feasible (need-session, teacher, slot, room)
  assignment, pre-filtered by availability/capability (same pruning as
  v1's `candidates`).
- Hard constraints as linear constraints: session coverage (== need),
  student ≤1 per slot, teacher ≤capacity per slot, room ≤capacity per
  slot, student ≤2 per day, consecutiveness via pairwise implication on
  same-day booleans, pinned lessons fixed to 1.
- Objectives as penalty terms with `SolverConfig` weights: two-lesson-day
  indicators, teacher-day indicators, |load − mean| deviation vars.
- Deterministic: fixed random seed, single worker, time limit parameter;
  fall back to v1 result if no improvement found in time.
- Acceptance: on `sample_data`, must reproduce or beat v1 on every
  objective term with identical hard-constraint validity (checked by the
  existing `validate` — which remains the source of truth, applied to
  whatever the model outputs).

### Phase 3 — minimal-disruption rescheduling

- `resolve(data, current, changed_inputs) -> SolveResult` with an extra
  objective term: number of lessons that differ from `current` (weighted
  high). Covers "teacher sick on 8/5", "student adds a need mid-term".
- UI: "Reschedule (keep as much as possible)" button next to Generate.

### Phase 4 — native fallback improvements (no ortools)
Only if ortools proves unacceptable as a dependency:

- Swap moves (2-opt between teachers/slots) in the local search.
- Large-neighborhood search: destroy a random day/teacher slice, re-run
  the exact backtracker on the fragment, keep if better.
- Branch-and-bound on the weighted objective inside `solve` itself.

## Invariants that must survive v2

- `validate` stays the single source of truth; v2 output is always run
  through it and must come back clean (or the result is rejected and v1's
  answer is used).
- Determinism: same input + config → same schedule, byte for byte.
- Coverage neutrality: exactly `sessions` lessons per (student, subject).
- Pinned (user-placed) lessons are never moved.
- The API/UI contract (`SolveResult`, `/api/schedule/generate`) is
  unchanged; v2 is selected by a request option, default off until proven.

## Test plan

- Golden tests: v2 with lexicographic preset ≥ v1 on every instance in
  the suite (never worse on any objective term, valid, complete).
- Property test: random small instances (≤6 students) — brute-force
  enumeration of optima vs v2 result.
- Time-limit behavior: v2 under a 1ms budget still returns a valid
  (possibly v1) schedule.
