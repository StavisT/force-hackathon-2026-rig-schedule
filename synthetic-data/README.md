# Synthetic data for the rig schedule optimizer

Built from `../synthetic_data_prompt.md` against spec v0.4 (`../rig_schedule_spec.md`).

| File | What it is |
|---|---|
| `generate_data.py` | Deterministic generator (numpy + pandas, seed 42 by default) |
| `validate_data.py` | Checks every rule in the prompt (§9) and spec (§12); exit code 1 on failure |
| `rigsched_common.py` | Shared rules (effective duration, sailing, compatibility) so both scripts agree |
| `data/<size>/` | Generated datasets: `small`, `default`, `large`, each also as `<size>_NOK_Sm3` |

Each dataset folder has its own `README.md` with summary statistics and the list of edge cases.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install numpy pandas
.venv/bin/python generate_data.py --size all --unit-set both
.venv/bin/python validate_data.py data/small data/default data/large data/default_NOK_Sm3
```

## Notes

- `reference_schedule.csv` is a greedy "last approved" baseline. It is feasible (validated) but not optimal,
  and it does not try to meet exploration targets. `frozen_schedule.csv` is its first 12 months.
- The greedy credits injectors with half the value of the producers they unlock; reported values in
  `wells.csv` (`value_at_plan_start_usd`) are each well's own value only.
- In later years the portfolio thins out and rigs run out of compatible work (about 2033–34 in `default`).
