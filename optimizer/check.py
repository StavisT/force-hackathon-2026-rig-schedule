#!/usr/bin/env python3
"""Independent acceptance checker (spec §13) for one optimizer output folder.

It reads only the dataset and the output ``schedule.csv`` / ``kpis.json`` /
``analysis.json``, and uses the data package's own shared rules
(``synthetic-data/rigsched_common.py``) for sailing, effective duration and
compatibility - not the optimizer's code.  Value is recomputed from scratch
with a plain month-by-month loop (criterion 7).

Usage:  python check.py --data <dataset> --run <output>/<variant> [--reference <dataset>/reference_schedule.csv]
Exit code 1 if any check fails.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "synthetic-data"))
from rigsched_common import Calendar, compatible, effective_duration, great_circle_nm, split_set  # noqa: E402

BOE_PER_SM3, MMBTU_PER_SM3_GAS, MMBTU_PER_BOE = 6.2898, 0.0353, 5.8
FREEDOM = {"minimal": (30, False, False, 15), "moderate": (120, True, True, None), "free": (None, True, True, None)}


def cfg_of(p: Path) -> dict:
    out = {}
    for line in p.read_text().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def blank(v):
    return v is None or (isinstance(v, float) and math.isnan(v)) or str(v) == ""


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


class Checker:
    def __init__(self):
        self.res = []

    def ok(self, crit, name, cond, detail=""):
        self.res.append((crit, name, bool(cond), detail))

    def report(self):
        bad = 0
        for crit, name, c, d in self.res:
            print(f"  [{"PASS" if c else "FAIL"}] {("§13." + str(crit)) if crit else "§5 rule":<8} {name}" + ("" if c or not d else f"  -> {d}"))
            bad += not c
        return bad


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--run", required=True, type=Path)
    a = ap.parse_args(argv)
    root, run = a.data, a.run
    C = Checker()
    cfg = cfg_of(root / "config.yaml")
    K = json.loads((run / "kpis.json").read_text())
    AN = json.loads((run / "analysis.json").read_text())
    nok_in = cfg.get("unit_set") == "NOK_Sm3"
    nok_out = K["unit_set"] == "NOK_Sm3"
    plan_start = date.fromisoformat(cfg["plan_start"])
    freeze_end = date.fromisoformat(K["freeze_end"])
    horizon_end = date.fromisoformat(K["horizon_end"])
    day = lambda s: (date.fromisoformat(str(s)) - plan_start).days
    FD, H = day(freeze_end), day(horizon_end)
    r_ = float(cfg.get("discount_rate", 0.08))

    W = pd.read_csv(root / "wells.csv", keep_default_na=False)
    R = pd.read_csv(root / "rigs.csv", keep_default_na=False)
    U = pd.read_csv(root / "rig_unavailability.csv", keep_default_na=False)
    PH = pd.read_csv(root / "well_phases.csv", keep_default_na=False)
    PR = pd.read_csv(root / "well_predecessors.csv", keep_default_na=False)
    BC = pd.read_csv(root / "batch_campaigns.csv", keep_default_na=False)
    T = pd.read_csv(root / "exploration_targets.csv", keep_default_na=False)
    WX = pd.read_csv(root / "weather.csv")
    PD = pd.read_csv(root / "price_deck.csv").sort_values("year")
    S = pd.read_csv(run / "schedule.csv", keep_default_na=False)
    S["s"], S["e"] = S.start_date.map(day), S.end_date.map(day)

    fx = float(PD.fx_nok_usd.iloc[0])
    wells = {r.well_id: r._asdict() for r in W.itertuples(index=False)}
    rigs = {}
    for r in R.itertuples(index=False):
        d = r._asdict()
        d["capabilities"] = split_set(d["capabilities"])
        d["unavail"] = [(day(u.start), day(u.end)) for u in U.itertuples() if u.rig_id == r.rig_id]
        rigs[r.rig_id] = d
    for w in wells.values():
        req = split_set(w["required_capabilities"])
        if str(w["is_hpht"]) == "True":
            req.add("HPHT")
        if w["category"] in ("exploration", "appraisal"):
            req |= {"WELL_TEST", "FORMATION_TEST"}
        w["required_capabilities"] = req
        for k in ("allowed_rig_types", "allowed_rigs", "excluded_rigs"):
            w[k] = split_set(w[k])
        w["water_depth_m"] = float(w["water_depth_m"]); w["measured_depth_m"] = float(w["measured_depth_m"])
    if K.get("rig_speed_knots"):
        for r in rigs.values():
            r["transit_speed_knots"] = K["rig_speed_knots"]
    for r in rigs.values():
        for k in ("min_water_depth_m", "max_water_depth_m", "max_drilling_depth_m", "transit_speed_knots",
                  "mob_demob_days", "move_sensitivity"):
            r[k] = float(r[k])
    wow = {reg: [float(WX[(WX.region == reg) & (WX.month == m)].wow_factor.iloc[0]) for m in range(1, 13)]
           for reg in WX.region.unique()}
    cal = Calendar(plan_start, (horizon_end - plan_start).days + 5000, wow)
    phases = {(p.well_id, p.phase): p for p in PH.itertuples()}
    tf = {"none": float(cfg.get("tier_factor_none", 1.0)), "avoid": float(cfg.get("tier_factor_avoid", 1.5)),
          "strongly_avoid": float(cfg.get("tier_factor_strongly_avoid", 3.5))}

    same_inst = K.get("same_installation_move_days")
    same_inst = int(cfg.get("same_installation_move_days", 1)) if same_inst is None else int(same_inst)
    mob_in = bool(K.get("mob_in_sailing", True))
    rounding = K.get("sail_rounding", "up")

    def sail_need(r, frm, to, depart):
        """Independent copy of the sailing rule recorded in kpis.json."""
        if frm["well_id"] == to["well_id"]:
            return 0
        if frm["installation_id"] and frm["installation_id"] == to["installation_id"]:
            return same_inst
        nm = great_circle_nm(frm["lat"], frm["lon"], to["lat"], to["lon"])
        days = (nm / (r["transit_speed_knots"] * 24.0) + (r["mob_demob_days"] if mob_in else 0.0)) * \
            (1.0 + r["move_sensitivity"] * cal.wow(to["region"], depart))
        return math.ceil(days - 1e-9) if rounding == "up" else math.floor(days + 0.5)

    def loc(wid):
        w = wells[wid]
        return dict(well_id=wid, installation_id=w["installation_id"] or None, lat=float(w["lat"]),
                    lon=float(w["lon"]), region=w["region"])

    # ---------------------------------------------------------------- 1, 2, 9 + rig rules
    e = defaultdict(list)
    for rid, g in S.groupby("rig_id"):
        r = rigs[rid]
        g = g.sort_values("s")
        prev = None
        for x in g.itertuples():
            w = wells[x.well_id]
            if not compatible(r, w):
                e["compat"].append(x.well_id)
            if ("HPHT" in w["required_capabilities"] and "HPHT" not in r["capabilities"]) or \
               ("WELL_TEST" in w["required_capabilities"] and "WELL_TEST" not in r["capabilities"]):
                e["hpht_test"].append(x.well_id)
            if x.s < day(r["contract_start"]) or x.e > day(r["contract_end"]):
                e["contract"].append(x.well_id)
            if any(x.s < b and a < x.e for a, b in r["unavail"]):
                e["unavail"].append(x.well_id)
            if x.phase == "full":
                base, sens = float(w["duration_p50_days"]), float(w["weather_sensitivity"])
            else:
                p = phases[(x.well_id, x.phase)]
                base, sens = float(p.duration_p50_days), float(p.weather_sensitivity)
            eff = effective_duration(cal, base, sens, w["winter_tier"], w["region"], x.s)
            if eff * 1.0 != x.e - x.s:
                e["eff"].append(x.well_id)
            if prev is not None:
                need = sail_need(r, loc(prev.well_id), loc(x.well_id), prev.e)
                both_frozen = str(x.is_frozen) == "True" and str(prev.is_frozen) == "True"
                if x.s < prev.e:
                    e["overlap"].append(x.well_id)
                elif x.s < prev.e + need and not both_frozen:
                    e["sail"].append(f"{prev.well_id}->{x.well_id}")
            if not str(x.is_frozen) == "True" and (x.s < FD or x.s >= H):
                e["zone"].append(x.well_id)
            prev = x
    C.ok(1, "no overlapping wells on a rig", not e["overlap"], e["overlap"][:5])
    C.ok(1, "gaps >= weather-adjusted sailing time", not e["sail"], e["sail"][:5])
    C.ok(2, "every assignment compatible with its rig", not e["compat"], e["compat"][:5])
    C.ok(9, "HPHT / test-equipment wells only on rigs that have it", not e["hpht_test"], e["hpht_test"][:5])
    C.ok(0, "inside rig contracts", not e["contract"], e["contract"][:5])
    C.ok(0, "outside rig unavailability", not e["unavail"], e["unavail"][:5])
    C.ok(0, "effective durations follow the weather rule", not e["eff"], e["eff"][:5])
    C.ok(4, "optimizer places nothing in the frozen period or after the horizon", not e["zone"], e["zone"][:5])

    # ---------------------------------------------------------------- 3 installations
    S["inst"] = S.well_id.map(lambda w: wells[w]["installation_id"] or f"@{wells[w]['lat']},{wells[w]['lon']}")
    clash = []
    for inst, g in S.groupby("inst"):
        g = g.sort_values("s").reset_index(drop=True)
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                if g.s[j] >= g.e[i]:
                    break
                if g.rig_id[i] != g.rig_id[j]:
                    clash.append(f"{inst}: {g.well_id[i]}/{g.well_id[j]}")
    C.ok(3, "one rig per installation at a time", not clash, clash[:5])

    # ---------------------------------------------------------------- 4 frozen
    fr_file = root / "frozen_schedule.csv"
    if int(cfg.get("frozen_months", 12)) == K["frozen_months"] and fr_file.exists():
        F = pd.read_csv(fr_file, keep_default_na=False)
        got = S[S.is_frozen.astype(str) == "True"]
        a_ = set(zip(F.well_id, F.phase, F.rig_id, F.start_date, F.end_date))
        b_ = set(zip(got.well_id, got.phase, got.rig_id, got.start_date, got.end_date))
        C.ok(4, "frozen assignments unchanged", a_ == b_, f"{len(a_ ^ b_)} differ")
    else:
        C.ok(4, "frozen assignments: frozen_months differs from dataset; derived from reference (not compared)", True)

    # ---------------------------------------------------------------- 5 windows, mandatory, strategic, precedence
    first, last = S.groupby("well_id").s.min(), S.groupby("well_id").e.max()
    win = []
    for wid in first.index:
        w = wells[wid]
        if str(S[S.well_id == wid].is_frozen.iloc[0]) == "True":
            continue
        if first[wid] < day(w["earliest_start"]):
            win.append(f"{wid} earliest")
        if not blank(w["latest_start"]) and first[wid] > day(w["latest_start"]):
            win.append(f"{wid} latest")
        if not blank(w["must_complete_by"]) and last[wid] > day(w["must_complete_by"]):
            win.append(f"{wid} complete-by")
        if str(w["is_strategic"]) == "True" and not blank(w["strategic_window_start"]):
            if first[wid] < day(w["strategic_window_start"]) or last[wid] > day(w["strategic_window_end"]):
                win.append(f"{wid} strategic window")
    C.ok(5, "time windows and strategic windows respected", not win, win[:5])
    missing = [wid for wid, w in wells.items() if (str(w["is_mandatory"]) == "True" or str(w["is_strategic"]) == "True")
               and wid not in first.index]
    reported = all(any(m in s for s in AN["infeasibility"]) for m in missing)
    C.ok(5, "mandatory/strategic wells scheduled, or infeasibility reported", not missing or reported,
         f"missing {missing[:5]}, reported={reported}")
    prec = [f"{p.predecessor_id}->{p.well_id}" for p in PR.itertuples() if p.well_id in first.index and
            (p.predecessor_id not in last.index or first[p.well_id] < last[p.predecessor_id] + int(p.min_lag_days))]
    C.ok(0, "precedence respected", not prec, prec[:5])

    # ---------------------------------------------------------------- 6 exploration targets
    sp = defaultdict(int)
    for wid in first.index:
        if wells[wid]["category"] in ("exploration", "appraisal"):
            sp[(plan_start + timedelta(days=int(first[wid]))).year] += 1
    off = [f"{t.year}: {sp[t.year]} not in [{t.min_wells},{t.max_wells}]" for t in T.itertuples()
           if not (t.min_wells <= sp[t.year] <= t.max_wells)]
    rep = all(any(str(t.split(':')[0]) in s for s in AN["infeasibility"]) for t in off)
    C.ok(6, "exploration wells per year within targets" + (" (deviations reported)" if off and rep else ""),
         not off or rep, off[:5])

    # ---------------------------------------------------------------- 12 batch
    bad12 = []
    for wid, g in S[S.phase != "full"].groupby("well_id"):
        th, mn = g[g.phase == "top_hole"], g[g.phase == "main"]
        if len(th) != 1 or len(mn) != 1:
            bad12.append(f"{wid} incomplete")
        elif mn.s.iloc[0] < th.e.iloc[0]:
            bad12.append(f"{wid} main before top hole")
    for b in BC.itertuples():
        ids = set(b.well_ids.split(";"))
        tops = S[(S.phase == "top_hole") & S.well_id.isin(ids)]
        if len(tops) == 0:
            continue
        if tops.rig_id.nunique() != 1 or len(tops) != len(ids):
            bad12.append(f"{b.batch_campaign_id} split")
            continue
        seq = S[S.rig_id == tops.rig_id.iloc[0]].sort_values("s").reset_index(drop=True)
        pos = seq.index[(seq.phase == "top_hole") & seq.well_id.isin(ids)]
        if pos.max() - pos.min() + 1 != len(pos):
            bad12.append(f"{b.batch_campaign_id} not consecutive")
    C.ok(12, "main after top hole; campaign top holes consecutive on one rig", not bad12, bad12[:5])

    # ---------------------------------------------------------------- 7 value recomputation
    prof = pd.read_csv(root / "production_profiles.csv")
    if nok_in:
        prof["oil_bbl"] = prof.oil_sm3 * BOE_PER_SM3
        prof["gas_mmbtu"] = prof.gas_sm3 * MMBTU_PER_SM3_GAS
        PD["oil_usd_bbl"] = PD.oil_nok_sm3 / PD.fx_nok_usd / BOE_PER_SM3
        PD["gas_usd_mmbtu"] = PD.gas_nok_sm3 / PD.fx_nok_usd / MMBTU_PER_SM3_GAS
    profd = {wid: g for wid, g in prof.groupby("well_id")}
    oilp = dict(zip(PD.year, PD.oil_usd_bbl)); gasp = dict(zip(PD.year, PD.gas_usd_mmbtu)); ylast = PD.year.max()
    tail = 12 * int(cfg.get("production_tail_years", 20))
    DF = lambda t: (1 + r_) ** (-t / 365.25)
    usd = lambda w, k: (float(w[k + "_nok"]) / fx) if nok_in else float(w[k + "_usd"])
    total, worst = 0.0, 0.0
    for wid in first.index:
        w = wells[wid]
        g = S[S.well_id == wid]
        end = int(g.e.max())
        capex = usd(w, "capex")
        cap = 0.0
        for x in g.itertuples():
            share = 1.0 if x.phase == "full" else float(phases[(wid, x.phase)].capex_share)
            cap += capex * share * DF(x.s)
        if w["category"] in ("exploration", "appraisal"):
            dv = usd(w, "discovery_value")
            v = float(w["probability_of_success"]) * dv * DF(end + 30.4375 * int(float(w["discovery_value_lag_months"]))) - cap
        else:
            if nok_in:
                of = float(w["opex_fixed_nok_per_year"]) / fx
                ov = float(w["opex_variable_nok_per_sm3oe"]) / fx / BOE_PER_SM3
            else:
                of, ov = float(w["opex_fixed_usd_per_year"]), float(w["opex_variable_usd_per_boe"])
            pr = profd.get(wid)
            mo = dict(zip(pr.month_index, zip(pr.oil_bbl, pr.gas_mmbtu))) if pr is not None else {}
            comp = plan_start + timedelta(days=end)
            v = 0.0
            for mth in range(1, tail + 1):
                o, gm = mo.get(mth, (0.0, 0.0))
                y = add_months(date(comp.year, comp.month, 1), mth).year
                y = min(y, ylast)
                cf = o * oilp[y] + gm * gasp[y] - of / 12 - ov * (o + gm / MMBTU_PER_BOE)
                v += cf * DF(end + 30.4375 * mth)
            v -= cap
        if nok_out:
            v *= fx
        rep_v = float(g[g.phase.isin(["full", "main"])].value.iloc[0])
        worst = max(worst, abs(v - rep_v) / max(1e6, abs(v)))
        total += v
    C.ok(7, "reported per-well value equals independent recomputation (rel. 1e-4)", worst < 1e-4, f"worst rel. {worst:.2e}")
    C.ok(7, "reported portfolio value equals independent recomputation", abs(total - K["portfolio_value"]) <= 1e-4 * abs(total),
         f"{total:,.0f} vs {K['portfolio_value']:,.0f}")

    # ---------------------------------------------------------------- 11 freedom limits
    lvl = K["optimization_freedom"]
    if lvl in FREEDOM and lvl != "free":
        shift, rigchg, addrm, maxch = FREEDOM[lvl]
        REF = pd.read_csv(root / "reference_schedule.csv", keep_default_na=False)
        REF["s"] = REF.start_date.map(day)
        REF = REF[REF.s >= FD]
        rs = REF.sort_values("s").groupby("well_id").first()
        ss = S[S.is_frozen.astype(str) != "True"].sort_values("s").groupby("well_id").first()
        viol = []
        for wid in set(rs.index) | set(ss.index):
            if wid in rs.index and wid in ss.index:
                if shift is not None and abs(ss.s[wid] - rs.s[wid]) > shift:
                    viol.append(f"{wid} shift {ss.s[wid] - rs.s[wid]}")
                if not rigchg and ss.rig_id[wid] != rs.rig_id[wid]:
                    viol.append(f"{wid} rig change")
            elif not addrm:
                mand = str(wells[wid]["is_mandatory"]) == "True"
                if not (wid in ss.index and mand):
                    viol.append(f"{wid} {'added' if wid in ss.index else 'removed'}")
        C.ok(11, f"optimization-freedom limits respected ({lvl})", not viol, viol[:5])

    # ---------------------------------------------------------------- 13 winter (informational from output)
    wint = set(int(x) for x in cfg.get("winter_months", "11,12,1,2").split(","))
    def wshare(x):
        d = [(plan_start + timedelta(days=k)).month in wint for k in range(int(x.s), int(x.e))]
        return sum(d) / max(1, len(d))
    sa = [x for x in S.itertuples() if str(x.is_frozen) != "True" and x.winter_tier == "strongly_avoid" and x.phase == "full"
          and wshare(x) >= 0.5]
    C.ok(13, f"strongly_avoid wells placed in winter: {len(sa)} (swap test runs in run_acceptance.py)", True)

    n_bad = C.report()
    print(f"{len(C.res) - n_bad} passed, {n_bad} failed")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
