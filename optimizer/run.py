#!/usr/bin/env python3
"""Rig schedule optimizer - command line.

Examples
--------
  python run.py --data ../synthetic-data/data/default --out ../output/default
  python run.py --data ../synthetic-data/data/default --out ../output/default-variants \
                --variants minimal,moderate,free
  python run.py --data ../synthetic-data/data/small --out ../output/small --frozen-months 6
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rigopt.analysis import Analysis  # noqa: E402
from rigopt.baseline import build_baseline  # noqa: E402
from rigopt.data import Model, Settings  # noqa: E402
from rigopt.decoder import Problem, decode  # noqa: E402
from rigopt.economics import Economics  # noqa: E402
from rigopt.report import write_report  # noqa: E402
from rigopt.search import Search  # noqa: E402


FOLDER = {"free": "optimized-full", "moderate": "optimized-medium", "minimal": "optimized-small"}
LABEL = {
    "free": ("Optimized – full re-plan",
             "The optimizer may move any well after the frozen period to any compatible rig and date, "
             "and add or drop wells."),
    "moderate": ("Optimized – medium changes",
                 "Each well in the last approved plan may move up to ±120 days and change rig; "
                 "wells may be added or dropped."),
    "minimal": ("Optimized – small adjustments",
                "Each well may move at most ±30 days and keeps its rig; no wells added or dropped; "
                "at most 15 wells changed."),
    "reference": ("Last approved plan",
                  "The supplied last approved schedule (a greedy plan), re-timed with the current sailing rules. "
                  "It does not try to meet exploration targets."),
    "baseline": ("Manual-style baseline",
                 "How a schedule is typically built by hand: when a rig is free it takes the next ready well "
                 "(first come, first served by earliest start), staying field by field, with some random "
                 "whitespace. Not optimized."),
}


def build(data: Path, args, freedom: str) -> tuple:
    cfg_unit = None
    s = Settings()
    m0 = Model(data)                       # read config defaults
    s.frozen_months = args.frozen_months if args.frozen_months is not None else m0.s.frozen_months
    s.unit_set = args.unit_set or m0.input_unit_set
    s.price_case = args.price_case
    s.seed = args.seed
    s.iterations = args.iterations
    s.locked_wells = tuple(x for x in (args.locked_wells or "").split(",") if x)
    s.rig_speed_knots = args.rig_speed_knots or None
    s.same_installation_move_days = args.same_installation_move_days
    s.mob_in_sailing = args.mob_in_sailing
    s.sail_rounding = args.sail_rounding
    s.apply_freedom(freedom)
    if args.max_shift_days is not None:
        s.max_shift_days = args.max_shift_days
    if args.max_wells_changed is not None:
        s.max_wells_changed = args.max_wells_changed
    if args.change_penalty_weight is not None:
        s.change_penalty_weight = args.change_penalty_weight
    m = Model(data, s)
    e = Economics(m)
    P = Problem(m, e)
    del cfg_unit
    return m, e, P


def solve(P: Problem, seed: int, iterations, warm=None, log=print):
    S = Search(P, seed=seed, log=log)
    extra = [warm] if warm is not None else []
    return S.run(iterations=iterations, extra_seeds=extra)


def save_variant(out: Path, name: str, P: Problem, seqs, res, analysis: dict):
    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    cols = ["well_id", "phase", "rig_id", "start_date", "end_date", "base_duration_days", "effective_duration_days",
            "sail_days_in", "is_frozen", "locked_by", "locked_at", "weather_added_days", "asset", "category",
            "purpose", "winter_tier", "value"]
    pd.DataFrame(analysis["schedule"])[cols].to_csv(d / "schedule.csv", index=False)
    pd.DataFrame(analysis["economics"]["per_well"]).to_csv(d / "well_economics.csv", index=False)
    pd.DataFrame(analysis["economics"]["yearly"]).to_csv(d / "yearly_economics.csv", index=False)
    pd.DataFrame(analysis["utilization"]).to_csv(d / "utilization.csv", index=False)
    pd.DataFrame(analysis["unscheduled"]).to_csv(d / "unscheduled.csv", index=False)
    if "comparison" in analysis:
        pd.DataFrame(analysis["comparison"]["rows"]).to_csv(d / "comparison_vs_reference.csv", index=False)
    (d / "kpis.json").write_text(json.dumps(analysis["kpis"], indent=2, default=float), encoding="utf-8")
    (d / "analysis.json").write_text(json.dumps(analysis, default=float), encoding="utf-8")
    sol = [[P.tasks[t].key for t in q] for q in seqs]
    (d / "solution.json").write_text(json.dumps(dict(rigs=[r.rig_id for r in P.m.rigs], sequences=sol), indent=1),
                                     encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path, help="dataset folder")
    ap.add_argument("--out", required=True, type=Path, help="output folder (created)")
    ap.add_argument("--variants", default=None,
                    help="comma list of freedom levels (minimal,moderate,free); default: config optimization_freedom")
    ap.add_argument("--n-variants", type=int, default=None, help="diverse variants per level (different seeds)")
    ap.add_argument("--frozen-months", type=int, default=None)
    ap.add_argument("--unit-set", choices=["USD_bbl_MMBtu", "NOK_Sm3"], default=None, help="output unit set")
    ap.add_argument("--price-case", choices=["base", "low", "high"], default="base")
    ap.add_argument("--iterations", type=int, default=None)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--locked-wells", default=None, help="comma list of well ids pinned to the reference")
    ap.add_argument("--max-shift-days", type=int, default=None)
    ap.add_argument("--max-wells-changed", type=int, default=None)
    ap.add_argument("--change-penalty-weight", type=float, default=None)
    ap.add_argument("--warm-start", type=Path, default=None, help="schedule.csv of an earlier run to start from")
    ap.add_argument("--mc-samples", type=int, default=200)
    ap.add_argument("--no-mc", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-baseline", action="store_true", help="skip the manual-style first-come-first-served baseline")
    ap.add_argument("--same-installation-move-days", type=int, default=0,
                    help="days to move between wells on the same platform/template (default 0)")
    ap.add_argument("--mob-in-sailing", action="store_true",
                    help="add each rig's mob/demob days to every move (old rule; default: part of well duration)")
    ap.add_argument("--sail-rounding", choices=["nearest", "up"], default="nearest")
    ap.add_argument("--rig-speed-knots", type=float, default=7.0,
                    help="transit speed for all rigs (default 7); 0 = per-rig speeds from rigs.csv")
    args = ap.parse_args(argv)
    log = (lambda *a: None) if args.quiet else print

    cfg = Model(args.data).cfg
    levels = (args.variants or cfg.get("optimization_freedom", "free")).split(",")
    n_var = args.n_variants or int(cfg.get("n_variants", 1))
    runs = []
    for lv in levels:
        for k in range(n_var):
            runs.append((lv if n_var == 1 else f"{lv}-{k + 1}", lv, args.seed + k))

    args.out.mkdir(parents=True, exist_ok=True)
    variants, summary = [], []
    ref_res_free = None
    for name, lv, seed in runs:
        t0 = time.time()
        m, e, P = build(args.data, args, lv)
        folder = FOLDER.get(name, name)
        log(f"[{folder}] {len(m.wells)} wells, {len(m.rigs)} rigs, {P.T} tasks; frozen until {m.freeze_end}; "
            f"freedom {lv}")
        ref = decode(P, P.ref_seqs)
        warm = None
        if args.warm_start:
            warm = P.seqs_from_df(pd.read_csv(args.warm_start))
        seqs, res = solve(P, seed, args.iterations, warm, log)
        A = Analysis(P, seqs, res, name=folder, ref_res=ref)
        A.label, A.description = LABEL.get(lv, (lv, ""))
        an = A.full(with_mc=not args.no_mc, mc_samples=args.mc_samples)
        an["kpis"].update(runtime_s=round(time.time() - t0, 1), seed=seed, kind="optimized")
        save_variant(args.out, folder, P, seqs, res, an)
        variants.append(an)
        summary.append(an["kpis"])
        log(f"[{folder}] value {an['kpis']['portfolio_value']/1e6:,.1f} M {an['kpis']['money_unit']}, "
            f"{an['kpis']['wells_scheduled']} wells, {time.time() - t0:.0f} s")
    # comparison schedules, explained without freedom limits
    _, _, Pf = build(args.data, args, "free")
    ref_f = decode(Pf, Pf.ref_seqs)
    RA = Analysis(Pf, Pf.ref_seqs, ref_f, name="last-approved")
    RA.label, RA.description = LABEL["reference"]
    ref_analysis = RA.full(with_mc=False, with_discovery=False)
    ref_analysis["kpis"]["kind"] = "last_approved"
    save_variant(args.out, "last-approved", Pf, Pf.ref_seqs, ref_f, ref_analysis)
    if not args.no_baseline:
        _, _, Pb = build(args.data, args, "free")
        bseqs, bpad, bres = build_baseline(Pb, seed=args.seed)
        BA = Analysis(Pb, bseqs, bres, name="baseline-fcfs", ref_res=decode(Pb, Pb.ref_seqs, pad={}))
        BA.label, BA.description = LABEL["baseline"]
        b_an = BA.full(with_mc=False, with_discovery=False)
        b_an["kpis"]["kind"] = "baseline"
        save_variant(args.out, "baseline-fcfs", Pb, bseqs, bres, b_an)
        log(f"[baseline-fcfs] value {b_an['kpis']['portfolio_value']/1e6:,.1f} M, {b_an['kpis']['wells_scheduled']} wells")
    else:
        b_an = None
    log(f"[last-approved] value {ref_analysis['kpis']['portfolio_value']/1e6:,.1f} M")
    meta = dict(dataset=str(args.data.name), generated=time.strftime("%Y-%m-%d %H:%M"),
                rigs=[dict(rig_id=r.rig_id, name=r.name, rig_type=r.rig_type, capabilities=sorted(r.capabilities),
                           contract_start=m.date(max(r.cs, -400)).isoformat(), contract_end=m.date(r.ce).isoformat(),
                           unavailability=[dict(start=m.date(a).isoformat(), end=m.date(b).isoformat(), reason=x)
                                           for a, b, x in r.unavail]) for r in m.rigs],
                winter_months=list(m.s.winter_months))
    meta["rig_speed_knots"] = m.s.rig_speed_knots
    meta["same_installation_move_days"] = m.s.same_installation_move_days
    others = [ref_analysis] + ([b_an] if b_an else [])
    (args.out / "summary.json").write_text(json.dumps(dict(meta=meta, variants=summary + [o["kpis"] for o in others]),
                                                      indent=2, default=float), encoding="utf-8")
    write_report(args.out / "report.html", meta, variants + others)
    log(f"Wrote {args.out}/report.html")


if __name__ == "__main__":
    main()
