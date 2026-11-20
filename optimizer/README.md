# Rig schedule optimizer (spec v0.4)

Python implementation of the problem in the Rig Schedule Optimizer spec, run against the
synthetic data in `../synthetic-data/data/`.

## Run

```bash
cd rig-schedule
/opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install numpy pandas   # once

# optimize (default freedom from config.yaml = free) and write CSV/JSON + report.html
.venv/bin/python optimizer/run.py --data synthetic-data/data/default --out output/default \
    --variants free,moderate,minimal

# independent acceptance check of one variant (spec §13), and the cross-run tests
.venv/bin/python optimizer/check.py --data synthetic-data/data/default --run output/default/free
.venv/bin/python optimizer/run_acceptance.py --data-root synthetic-data/data --out output/acceptance
```

Useful options: `--frozen-months N`, `--unit-set NOK_Sm3`, `--price-case low|high`,
`--locked-wells A,B`, `--max-shift-days`, `--max-wells-changed`, `--change-penalty-weight`,
`--n-variants K` (K diverse schedules per level), `--warm-start <schedule.csv>`, `--iterations`, `--seed`.

## Schedules produced (one folder each)

| Folder | Name in the report | What it is |
|---|---|---|
| `optimized-full` | Optimized – full re-plan | any well, any compatible rig and date after the frozen period; wells may be added/dropped |
| `optimized-medium` | Optimized – medium changes | ±120 days per well, rig changes and add/drop allowed |
| `optimized-small` | Optimized – small adjustments | ±30 days, same rig, no add/drop, ≤15 wells changed |
| `last-approved` | Last approved plan | the supplied reference schedule, re-timed with the current sailing rules |
| `baseline-fcfs` | Manual-style baseline | first come, first served by ready date (earliest start), field by field, random 3–14 day whitespace before ~⅓ of wells; skips clearly negative wells but keeps injectors that wanted wells depend on (`rigopt/baseline.py`) |

**Sailing** (default since v3) = transit only: distance ÷ (7 knots × 24 h) × (1 + rig move sensitivity × WoW factor
at the destination), rounded to the nearest whole day, so moves under 12 h are 0 days. Rig down/up (mob/demob) is part of
each well's duration and is *not* added to moves. Next well on the same platform/template: 0 days. Templates in one
field are 1–5 nm apart, so in-field moves are 0 days; between assets (median ~210 nm) 1–2 days.
Options: `--rig-speed-knots` (0 = per-rig speeds from rigs.csv), `--same-installation-move-days`, `--mob-in-sailing`
and `--sail-rounding up` restore the earlier rule (that was the data package's convention: +3–5 days mob/demob per
move, rounded up, 1 day on the same installation). Frozen dates are kept; their sailing is re-reported with the
current rule, and frozen-to-frozen gaps are not re-checked (admin-fixed input).

## Outputs (per schedule folder)

| File | Content |
|---|---|
| `schedule.csv` | Assignment rows (spec §4.7) + weather-added days, tier, value |
| `well_economics.csv`, `yearly_economics.csv` | NPV/EMV per well; yearly revenue, OPEX, CAPEX, oil, gas, boe |
| `utilization.csv` | drilling / weather-added / sailing / idle / unavailable days per rig |
| `unscheduled.csv` | every unscheduled well with reason (spec §9) and detail |
| `comparison_vs_reference.csv` | moved / rig change / added / dropped, shift and value delta |
| `kpis.json`, `analysis.json` | KPIs; everything incl. weather per winter, campaigns, discovery risk, Monte Carlo |
| `solution.json` | per-rig task sequences |
| `../report.html` | self-contained report: schedule (Gantt) first, Overview button, other views in the ☰ menu |

## How it works

* **Tasks.** A whole well (`W`), a conductor campaign = all its top holes consecutively (`C`), or the
  main phase of a phased well (`M`, any compatible rig, after its campaign).
* **Decoder** (`rigopt/decoder.py`). Given an ordered task list per rig, places tasks in near-chronological
  order across rigs at the earliest day that satisfies every hard rule: compatibility, sailing
  (weather-adjusted), contracts and unavailability, frozen period, time windows, precedence (across rigs),
  one rig per installation, strategic windows, horizon. Effective duration and sailing follow the data
  package's conventions (`synthetic-data/rigsched_common.py`); the decoder reproduces the supplied
  reference schedule exactly from its sequences. Unplaceable tasks are dropped with a reason.
* **Objective** (spec §8): Σ NPV (development, incl. injector incremental profiles) + Σ EMV (exploration)
  − winter penalty − batch penalty (gap/day − same-rig bonus) − tie-breaks (sailing, idle days), with large
  penalties for missing mandatory wells, exploration targets outside min–max and freedom-limit breaches.
* **Search** (`rigopt/search.py`). Starts from the reference (last approved) schedule, repairs mandatory
  wells and exploration targets, then seeded simulated annealing over relocate / swap / cross-rig move /
  cross-rig swap / reverse / insert (with missing predecessors) / remove moves, each fully re-decoded
  (~1 ms for 300 wells). Ends with best-insertion, adjacent-swap hill climb and the §13.13 winter swaps.
  Iteration-based, so the same input and seed give the same output.
* **Optimization freedom** (spec §8.5): `max_shift_days` becomes a hard start window around the reference
  date, `allow_rig_change=false` pins the reference rig, `allow_add_remove_wells=false` makes reference
  wells required and others non-insertable, `locked_wells` pin rig and start date. Presets: minimal
  (±30 d, no rig change, no add/remove, ≤15 changed wells), moderate (±120 d), free.

## Value definition (exact, checked independently by `check.py`)

DF(t) = (1+r)^(−t/365.25), t in days from plan start. Production month m (1..240) of a well completed on
day E is priced at the year of calendar month month(E)+m and discounted at E + 30.4375·m. OPEX = fixed/12 +
variable × boe for all 240 months. CAPEX is discounted at the start of each phase (top hole 15 %, main 85 %
for phased wells). EMV = PoS × discovery value × DF(E + 30.4375·lag) − CAPEX × DF(start).
Values in NOK use the plan-start FX rate from the price deck.

## Assumptions / simplifications

* All phases must start before the horizon end (the spec allows a main phase after the horizon; not used).
* Batch-drilling defaults: same-rig bonus USD 2 M per main phase, gap penalty USD 20 k/day (not in config.yaml).
* Tie-break weights: USD 10 k per sailing day, USD 1 k per idle day.
* A well counts as "changed" against the reference if it moved > 14 days, changed rig, or was added/dropped.
* Monte Carlo: fixed order per rig, durations from a piecewise-linear P10/P50/P90 distribution, weather factor
  scaled per region by a lognormal (σ 0.25); wells are not dropped in samples.
* Explanations use a fixed order: no compatible rig → freedom limit → time window → precedence →
  negative value → installation conflict → no capacity.
* The search is a heuristic: it finds good schedules, not proven optima. Criterion §13.8 is guaranteed by
  warm-starting a shorter-freeze run from the longer-freeze solution (`--warm-start`).
* "Estimated WoW cost" in the report = weather-added rig days × the well's rig day cost (full cost). The optimizer
  itself steers with the tiered winter penalty (0 / 10 / 50 % of that cost).
* Exploration-target repair may give a well a "not before" date (1 Jan of a short year) so it spuds in that year.
