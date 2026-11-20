#!/usr/bin/env python3
"""Cross-run acceptance tests (spec §13.8, 13.10, 13.13, 13.14) plus check.py on every run.

  python run_acceptance.py --data-root ../synthetic-data/data --out ../output/acceptance [--iterations 4000]

Uses a reduced iteration budget so it finishes in a few minutes.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PY = sys.executable


def run(data, out, *extra):
    cmd = [PY, str(HERE / "run.py"), "--data", str(data), "--out", str(out), "--quiet", "--no-mc", "--no-baseline", *map(str, extra)]
    subprocess.run(cmd, check=True)


def check(data, run_dir) -> bool:
    p = subprocess.run([PY, str(HERE / "check.py"), "--data", str(data), "--run", str(run_dir)],
                       capture_output=True, text=True)
    last = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else p.stderr[-300:]
    print(f"    check.py {run_dir.parent.name}/{run_dir.name}: {last}")
    if p.returncode:
        print("\n".join(l for l in p.stdout.splitlines() if "FAIL" in l))
    return p.returncode == 0


def kp(d):
    return json.loads((d / "kpis.json").read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--iterations", type=int, default=4000)
    a = ap.parse_args()
    D, O, it = a.data_root, a.out, a.iterations
    results = []

    def res(crit, name, ok, detail=""):
        results.append((crit, name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] §13.{crit} {name}" + (f"  ({detail})" if detail else ""))

    print("Determinism (§13.10): default dataset, same seed twice")
    run(D / "default", O / "det1", "--iterations", it)
    run(D / "default", O / "det2", "--iterations", it)
    s1 = pd.read_csv(O / "det1/optimized-full/schedule.csv"); s2 = pd.read_csv(O / "det2/optimized-full/schedule.csv")
    same = s1.equals(s2) and kp(O / "det1/optimized-full")["objective"] == kp(O / "det2/optimized-full")["objective"]
    res(10, "same input -> same output", same)
    ok_all = check(D / "default", O / "det1/optimized-full")

    print("Unit sets (§13.14): default vs default_NOK_Sm3")
    run(D / "default_NOK_Sm3", O / "nok", "--iterations", it)
    run(D / "default_NOK_Sm3", O / "nok_usd", "--iterations", it, "--unit-set", "USD_bbl_MMBtu")
    sn = pd.read_csv(O / "nok/optimized-full/schedule.csv"); su = pd.read_csv(O / "nok_usd/optimized-full/schedule.csv")
    cols = ["well_id", "phase", "rig_id", "start_date", "end_date"]
    same_sched = s1[cols].equals(sn[cols]) and s1[cols].equals(su[cols])
    kn, ku = kp(O / "nok/optimized-full"), kp(O / "det1/optimized-full")
    fx = 10.5
    rel = abs(kn["portfolio_value"] / fx - ku["portfolio_value"]) / abs(ku["portfolio_value"])
    rel2 = abs(kp(O / "nok_usd/optimized-full")["portfolio_value"] - ku["portfolio_value"]) / abs(ku["portfolio_value"])
    res(14, "identical schedule under USD_bbl_MMBtu and NOK_Sm3 input", same_sched)
    res(14, "values identical after conversion", rel < 1e-6 and rel2 < 1e-6, f"rel diff {rel:.1e} / {rel2:.1e}")
    ok_all &= check(D / "default_NOK_Sm3", O / "nok/optimized-full")

    print("Frozen period (§13.8): frozen_months 12 -> 6 must not reduce value")
    run(D / "default", O / "frz6", "--iterations", it, "--frozen-months", 6, "--warm-start", O / "det1/optimized-full/schedule.csv")
    v12, v6 = kp(O / "det1/optimized-full")["objective"], kp(O / "frz6/optimized-full")["objective"]
    res(8, "objective(frozen 6) >= objective(frozen 12)", v6 >= v12 - 1, f"{v6/1e6:,.1f} M vs {v12/1e6:,.1f} M")
    run(D / "default", O / "frz0", "--iterations", it, "--frozen-months", 0, "--warm-start", O / "frz6/optimized-full/schedule.csv")
    v0 = kp(O / "frz0/optimized-full")["objective"]
    res(8, "objective(frozen 0) >= objective(frozen 6)", v0 >= v6 - 1, f"{v0/1e6:,.1f} M vs {v6/1e6:,.1f} M")
    ok_all &= check(D / "default", O / "frz6/optimized-full")

    print("Winter swap (§13.13) on the determinism run")
    from rigopt.data import Model, Settings
    from rigopt.decoder import Problem, decode
    from rigopt.economics import Economics
    s = Settings()
    m = Model(D / "default", s); e = Economics(m); P = Problem(m, e)
    seqs = P.seqs_from_df(s1)
    base = decode(P, seqs)
    n_sa = n_try = n_better = 0
    for ri, q in enumerate(seqs):
        for tid in q:
            t = P.tasks[tid]
            if t.kind != "W" or tid not in base.placed:
                continue
            w = m.wells[t.wells[0]]
            segs = base.placed[tid][1]
            a_, b_ = segs[0][2], segs[-1][3]
            if w.tier != "strongly_avoid" or sum(m.is_winter(d) for d in range(a_, b_)) < 0.5 * (b_ - a_):
                continue
            n_sa += 1
            for oid in q:
                o = P.tasks[oid]
                if o.kind != "W" or oid == tid:
                    continue
                ow = m.wells[o.wells[0]]
                if ow.tier != "none" or abs(ow.base - w.base) > 0.25 * w.base:
                    continue
                s2 = [list(x) for x in seqs]
                i, j = s2[ri].index(tid), s2[ri].index(oid)
                s2[ri][i], s2[ri][j] = oid, tid
                r2 = decode(P, s2)
                if r2.dropped:
                    continue
                n_try += 1
                n_better += r2.objective > base.objective
    res(13, "no feasible improving swap of a winter strongly_avoid well with a similar 'none' well on its rig",
        n_better == 0, f"{n_sa} strongly_avoid wells in winter, {n_try} feasible swaps tested, {n_better} improving")

    print("Optimization freedom (§13.11): minimal + locked wells")
    ref = pd.read_csv(D / "default/reference_schedule.csv")
    post = ref[(ref.start_date >= "2028-03-01") & (ref.phase == "full")]
    locked = ",".join(post.well_id.iloc[[0, 10, 20]])
    run(D / "default", O / "minimal", "--iterations", it, "--variants", "minimal,moderate", "--locked-wells", locked)
    ok_all &= check(D / "default", O / "minimal/optimized-small")
    ok_all &= check(D / "default", O / "minimal/optimized-medium")
    sm = pd.read_csv(O / "minimal/optimized-medium/schedule.csv")
    pins = all(((sm.well_id == w) & sm.rig_id.isin(ref[ref.well_id == w].rig_id) &
                sm.start_date.isin(ref[ref.well_id == w].start_date)).any() for w in locked.split(","))
    res(11, f"locked wells kept in place ({locked})", pins)

    bad = sum(1 for r in results if not r[2])
    print(f"\n{len(results) - bad}/{len(results)} cross-run tests passed; check.py on all runs: "
          f"{'all passed' if ok_all else 'FAILURES'}")
    sys.exit(1 if bad or not ok_all else 0)


if __name__ == "__main__":
    main()
