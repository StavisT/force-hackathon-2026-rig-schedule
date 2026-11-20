#!/usr/bin/env python3
"""Validate a generated dataset against the synthetic-data prompt (§9) and spec v0.4 (§12).

Usage:
    python validate_data.py data/default [data/small ...]
Exit code 1 if any check fails.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from rigsched_common import (
    BOE_PER_SM3, MMBTU_PER_SM3_GAS, REGION_BOX, Calendar, compatible, effective_duration,
    sail_days, split_set,
)

ASSET_SHARE = {"Valhall": 0.20, "Fenris": 0.05, "Alvheim": 0.18, "Ivar Aasen": 0.08,
               "Edvard Grieg": 0.10, "Skarv": 0.12, "Yggdrasil": 0.27}
TIER_TARGET = {"none": 0.35, "avoid": 0.45, "strongly_avoid": 0.20}
EXPECTED_TAGS = ["hpht_bottleneck", "test_rig_bottleneck", "no_compatible_rig", "installation_conflict",
                 "same_asset_parallel", "strategic_window", "winter_sensitive", "winter_filler",
                 "conductor_campaign", "precedence_chain", "negative_value", "frozen_overrun", "rig_contract_end"]


class Check:
    def __init__(self):
        self.errors, self.warnings, self.passed = [], [], []

    def ok(self, cond, name, detail=""):
        (self.passed if cond else self.errors).append(name if cond else f"{name}: {detail}")
        return cond

    def warn(self, msg):
        self.warnings.append(msg)


def read_cfg(path: Path) -> dict:
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def d(s) -> date:
    return date.fromisoformat(str(s))


def is_blank(v) -> bool:
    return v is None or (isinstance(v, float) and np.isnan(v)) or str(v) == ""


def validate(root: Path) -> Check:
    c = Check()
    cfg = read_cfg(root / "config.yaml")
    if cfg.get("unit_set") == "NOK_Sm3":
        return validate_units(root, cfg, c)
    size = cfg["size"]
    plan_start, freeze_end, horizon_end = d(cfg["plan_start"]), d(cfg["freeze_end"]), d(cfg["horizon_end"])
    day = lambda x: (d(x) - plan_start).days
    W = pd.read_csv(root / "wells.csv", keep_default_na=False)
    R = pd.read_csv(root / "rigs.csv", keep_default_na=False)
    U = pd.read_csv(root / "rig_unavailability.csv")
    F = pd.read_csv(root / "fields.csv")
    I = pd.read_csv(root / "installations.csv")
    P = pd.read_csv(root / "well_predecessors.csv")
    PH = pd.read_csv(root / "well_phases.csv")
    BC = pd.read_csv(root / "batch_campaigns.csv")
    PR = pd.read_csv(root / "production_profiles.csv")
    T = pd.read_csv(root / "exploration_targets.csv", keep_default_na=False)
    WX = pd.read_csv(root / "weather.csv")
    PD = pd.read_csv(root / "price_deck.csv")
    REF = pd.read_csv(root / "reference_schedule.csv")
    FRZ = pd.read_csv(root / "frozen_schedule.csv")
    CM = pd.read_csv(root / "compatibility_matrix.csv")

    num = lambda col: pd.to_numeric(W[col].replace("", np.nan))
    wells = {}
    for r in W.to_dict("records"):
        r["required_capabilities"] = split_set(r["required_capabilities"])
        r["allowed_rig_types"] = split_set(r["allowed_rig_types"])
        r["allowed_rigs"] = split_set(r["allowed_rigs"])
        r["excluded_rigs"] = split_set(r["excluded_rigs"])
        r["tags"] = split_set(r["test_tag"])
        wells[r["well_id"]] = r
    rigs = {}
    for r in R.to_dict("records"):
        r["capabilities"] = split_set(r["capabilities"])
        r["unavail"] = [(day(u.start), day(u.end)) for u in U[U.rig_id == r["rig_id"]].itertuples()]
        rigs[r["rig_id"]] = r
    expl = W[W.category != "development"]
    dev = W[W.category == "development"]

    # 1. durations, PoS
    p10, p50, p90 = num("duration_p10_days"), num("duration_p50_days"), num("duration_p90_days")
    c.ok(((p10 <= p50) & (p50 <= p90)).all(), "P10 <= P50 <= P90")
    c.ok((pd.to_numeric(expl.discovery_duration_p50_days) > pd.to_numeric(expl.duration_p50_days)).all(),
         "discovery duration > dry-hole duration")
    pos = pd.to_numeric(expl.probability_of_success)
    c.ok(((pos >= 0) & (pos <= 1)).all(), "0 <= PoS <= 1")

    bad = [wid for wid, w in wells.items() if (str(w["is_hpht"]) == "True") != ("HPHT" in w["required_capabilities"])]
    c.ok(not bad, "is_hpht <=> HPHT in required_capabilities", str(bad[:5]))

    # 2. geography
    fr = F.set_index("field_id")
    bad = [w["well_id"] for w in wells.values() if w["category"] == "development"
           and not fr.loc[w["field_id"], "wd_min"] <= w["water_depth_m"] <= fr.loc[w["field_id"], "wd_max"]]
    c.ok(not bad, "development water depth within field range", str(bad[:5]))
    bad = [w["well_id"] for w in wells.values()
           if not (REGION_BOX[w["region"]][0] <= w["lat"] <= REGION_BOX[w["region"]][1]
                   and REGION_BOX[w["region"]][2] <= w["lon"] <= REGION_BOX[w["region"]][3])]
    c.ok(not bad, "coordinates inside region box", str(bad[:5]))

    # 3. test equipment
    c.ok(all("WELL_TEST" in w["required_capabilities"] for w in wells.values() if w["category"] != "development"),
         "all exploration/appraisal wells require WELL_TEST")
    n_test = sum("WELL_TEST" in r["capabilities"] for r in rigs.values())
    if size == "default":
        c.ok(n_test == 2, "exactly two test-equipped rigs (default size)", f"found {n_test}")
    else:
        c.ok(1 <= n_test <= max(1, round(0.3 * len(rigs))), "test-equipped rigs scarce (~25%)", f"found {n_test}/{len(rigs)}")

    # 4-5. compatibility
    comp = {wid: {rid for rid, r in rigs.items() if compatible(r, w)} for wid, w in wells.items()}
    bad = [wid for wid, s in comp.items() if not s and "no_compatible_rig" not in wells[wid]["tags"]]
    c.ok(not bad, "every well (except no_compatible_rig) has >= 1 compatible rig", str(bad[:5]))
    bad = [wid for wid, s in comp.items() if s and "no_compatible_rig" in wells[wid]["tags"]]
    c.ok(not bad, "no_compatible_rig wells really have no rig", str(bad))
    cm = CM.set_index("well_id")
    bad = [wid for wid in wells for rid in rigs if bool(cm.loc[wid, rid]) != (rid in comp[wid])]
    c.ok(not bad, "compatibility_matrix.csv matches recomputation", str(bad[:5]))

    # weather calendar
    wow = {r: [float(WX[(WX.region == r) & (WX.month == m)].wow_factor.iloc[0]) for m in range(1, 13)]
           for r in REGION_BOX}
    order_ok = all(wow["north_sea_south"][m] <= wow["north_sea_north"][m] <= wow["norwegian_sea"][m]
                   <= wow["barents_sea"][m] for m in range(12))
    c.ok(order_ok, "weather: south <= north <= Norwegian Sea <= Barents in every month")
    winter = [0, 1, 10, 11]
    c.ok(all(min(wow[r][m] for m in winter) > max(wow[r][m] for m in (5, 6, 7)) for r in wow),
         "weather: Nov-Feb worse than Jun-Aug in every region")
    cal = Calendar(plan_start, (horizon_end - plan_start).days + 4000, wow)
    move = int(cfg["same_installation_move_days"])

    # 6. schedules
    phases = {(r.well_id, r.phase): r for r in PH.itertuples()}

    def loc(wid):
        w = wells[wid]
        return dict(well_id=wid, installation_id=w["installation_id"] or None, lat=w["lat"], lon=w["lon"],
                    region=w["region"])

    def check_schedule(S: pd.DataFrame, label: str):
        S = S.copy()
        S["s"], S["e"] = S.start_date.map(day), S.end_date.map(day)
        errs = defaultdict(list)
        for rid, g in S.groupby("rig_id"):
            r = rigs[rid]
            g = g.sort_values("s")
            prev = None
            for a in g.itertuples():
                w = wells[a.well_id]
                if rid not in comp[a.well_id]:
                    errs["compatibility"].append(a.well_id)
                if a.s < day(r["contract_start"]) or a.e > day(r["contract_end"]):
                    errs["contract"].append(a.well_id)
                if any(a.s < ue and us < a.e for us, ue in r["unavail"]):
                    errs["unavailability"].append(a.well_id)
                base = w["duration_p50_days"] if a.phase == "full" else phases[(a.well_id, a.phase)].duration_p50_days
                sens = w["weather_sensitivity"] if a.phase == "full" else phases[(a.well_id, a.phase)].weather_sensitivity
                if abs(a.base_duration_days - base) > 1e-6:
                    errs["base duration"].append(a.well_id)
                eff = effective_duration(cal, base, sens, w["winter_tier"], w["region"], a.s)
                if eff != a.e - a.s or eff != a.effective_duration_days:
                    errs["effective duration (weather)"].append(a.well_id)
                if prev is None:
                    if a.s >= 0 and r["initial_well_id"] == "":
                        frm = dict(well_id=None, installation_id=None, lat=r["initial_lat"], lon=r["initial_lon"],
                                   region="north_sea_north")
                        dep = max(0, day(r["contract_start"]))
                        need = sail_days(cal, r, frm, loc(a.well_id), dep, move)
                        if a.s < dep + need:
                            errs["sailing from initial location"].append(a.well_id)
                else:
                    need = sail_days(cal, r, loc(prev.well_id), loc(a.well_id), prev.e, move)
                    if a.s < prev.e:
                        errs["overlap on rig"].append(a.well_id)
                    elif a.s < prev.e + need:
                        errs["sailing time"].append(a.well_id)
                    if a.sail_days_in != need:
                        errs["sail_days_in value"].append(a.well_id)
                prev = a
        # one rig per installation
        S["inst"] = S.well_id.map(lambda x: wells[x]["installation_id"])
        for inst, g in S[S.inst != ""].groupby("inst"):
            g = g.sort_values("s")
            ends = g.e.cummax().shift()
            bad = g[(g.s < ends) & (g.rig_id != g.rig_id.shift())]
            errs["two rigs on one installation"] += list(bad.well_id)
        # windows, precedence, phases
        first = S.groupby("well_id").s.min()
        last = S.groupby("well_id").e.max()
        for wid in first.index:
            w = wells[wid]
            if first[wid] >= 0 and first[wid] < day(w["earliest_start"]):
                errs["earliest_start"].append(wid)
            if w["latest_start"] and first[wid] > day(w["latest_start"]):
                errs["latest_start"].append(wid)
            if w["must_complete_by"] and last[wid] > day(w["must_complete_by"]):
                errs["must_complete_by"].append(wid)
            if w["strategic_window_start"] and (first[wid] < day(w["strategic_window_start"])
                                                or last[wid] > day(w["strategic_window_end"])):
                errs["strategic window"].append(wid)
        for p in P.itertuples():
            if p.well_id in first.index:
                if p.predecessor_id not in last.index:
                    errs["precedence (predecessor missing)"].append(p.well_id)
                elif first[p.well_id] < last[p.predecessor_id] + p.min_lag_days:
                    errs["precedence lag"].append(p.well_id)
        ph = S[S.phase != "full"]
        for wid, g in ph.groupby("well_id"):
            if set(g.phase) != {"top_hole", "main"}:
                errs["phased well missing a phase"].append(wid)
            elif g[g.phase == "main"].s.iloc[0] < g[g.phase == "top_hole"].e.iloc[0]:
                errs["main before top hole"].append(wid)
        for b in BC.itertuples():
            ids = set(b.well_ids.split(";"))
            tops = S[(S.phase == "top_hole") & S.well_id.isin(ids)]
            if len(tops) == 0:
                continue
            if tops.rig_id.nunique() != 1:
                errs["campaign top holes on several rigs"].append(b.batch_campaign_id)
                continue
            seq = S[S.rig_id == tops.rig_id.iloc[0]].sort_values("s").reset_index(drop=True)
            pos_ = seq.index[(seq.phase == "top_hole") & seq.well_id.isin(ids)]
            if len(pos_) != len(ids) or pos_.max() - pos_.min() + 1 != len(pos_):
                errs["campaign top holes not consecutive"].append(b.batch_campaign_id)
        for k in ["compatibility", "contract", "unavailability", "base duration", "effective duration (weather)",
                  "sailing from initial location", "overlap on rig", "sailing time", "sail_days_in value",
                  "two rigs on one installation", "earliest_start", "latest_start", "must_complete_by",
                  "strategic window", "precedence (predecessor missing)", "precedence lag",
                  "phased well missing a phase", "main before top hole", "campaign top holes on several rigs",
                  "campaign top holes not consecutive"]:
            c.ok(not errs[k], f"{label}: {k}", str(errs[k][:5]))

    check_schedule(REF, "reference schedule")
    check_schedule(FRZ, "frozen schedule")
    fd = (freeze_end - plan_start).days
    c.ok((FRZ.start_date.map(day) < fd).all() and FRZ.is_frozen.all(), "frozen schedule only starts inside frozen period")
    ref_frozen = REF[REF.start_date.map(day) < fd]
    c.ok(len(ref_frozen) == len(FRZ) and set(ref_frozen.well_id + ref_frozen.phase) == set(FRZ.well_id + FRZ.phase),
         "reference schedule identical to frozen schedule inside frozen period")
    st_bad = [wid for wid, w in wells.items() if w["status"] in ("frozen", "in_progress") and wid not in set(FRZ.well_id)]
    c.ok(not st_bad, "frozen / in_progress wells appear in frozen schedule", str(st_bad[:5]))
    mand = [wid for wid, w in wells.items() if str(w["is_mandatory"]) == "True" and wid not in set(REF.well_id)]
    if mand:
        c.warn(f"mandatory wells not in greedy reference schedule (allowed, optimizer must place them): {mand}")

    # 7. precedence acyclic
    g = defaultdict(list)
    for p in P.itertuples():
        g[p.predecessor_id].append(p.well_id)
    state = {}

    def dfs(u):
        state[u] = 1
        for v in g[u]:
            if state.get(v) == 1 or (state.get(v) is None and not dfs(v)):
                return False
        state[u] = 2
        return True
    c.ok(all(dfs(u) for u in list(g) if state.get(u) is None), "precedence graph acyclic")
    c.ok(set(P.well_id) | set(P.predecessor_id) <= set(wells), "precedence references known wells")

    # 8-9. structure
    if size != "small":
        per_inst = dev.groupby("installation_id").size()
        c.ok((per_inst >= 2).all(), "each installation has >= 2 wells", str(per_inst[per_inst < 2].to_dict()))
        n_inst = I.groupby("asset_id").size()
        multi = {a: n for a, n in n_inst.items() if a not in ("IVA", "FEN")}
        c.ok(all(n >= 2 for n in multi.values()), "each asset except Ivar Aasen and Fenris has >= 2 installations", str(multi))
    share = dev.asset.value_counts(normalize=True)
    bad = {a: round(share.get(a, 0), 3) for a, t in ASSET_SHARE.items() if abs(share.get(a, 0) - t) > 0.03}
    c.ok(not bad, "asset shares within ±3% of target", str(bad))

    # 10. exploration targets
    ok_expl = expl[~expl.test_tag.str.contains("no_compatible_rig")]
    c.ok(T.min_wells.sum() <= len(ok_expl), "sum of yearly minimums <= available exploration wells",
         f"{T.min_wells.sum()} > {len(ok_expl)}")
    cum, bad = 0, []
    for t in T.sort_values("year").itertuples():
        cum += t.min_wells
        avail = (ok_expl.earliest_start.map(d) <= date(t.year, 12, 31)).sum()
        if cum > avail:
            bad.append(t.year)
    c.ok(not bad, "cumulative minimums achievable given earliest starts", str(bad))
    cap = n_test * 365 / (pd.to_numeric(ok_expl.duration_p50_days).mean() + 10)
    c.ok(T.min_wells.max() <= cap, "yearly minimum within test-rig capacity", f"max min {T.min_wells.max()} > {cap:.1f}")
    c.ok((T.min_wells <= T.max_wells).all(), "min_wells <= max_wells")
    c.ok((T.test_tag == "binding_target").sum() >= 1, "a binding exploration target year exists")

    # 11. production profiles
    c.ok((PR[["oil_bbl", "gas_mmbtu"]] >= 0).all().all(), "production profiles non-negative")
    inc = PR.groupby("well_id").is_incremental.first()
    non_prod = dev[dev.purpose != "producer"].well_id
    c.ok(inc.reindex(non_prod).fillna(False).all(), "injectors/observation wells flagged incremental")
    boe = PR.assign(boe=PR.oil_bbl + PR.gas_mmbtu / 5.8)
    bad = []
    for wid, gp in boe[boe.well_id.isin(dev[dev.purpose == "producer"].well_id)].groupby("well_id"):
        q = gp.sort_values("month_index").boe.values
        k = int(np.argmax(q))
        if np.any(np.diff(q[k:]) > 1e-6 * q[k] + 0.2):
            bad.append(wid)
    c.ok(not bad, "producer profiles decline after plateau", str(bad[:5]))
    eur = boe.groupby("well_id").boe.sum() / 1e6
    prods = dev[(dev.purpose == "producer") & ~dev.test_tag.str.contains("negative_value")].well_id
    e = eur.reindex(prods)
    c.ok(((e >= 0.99) & (e <= 15.01)).all(), "producer EUR within 1-15 MMboe", str(e[(e < 0.99) | (e > 15.01)].head().to_dict()))
    c.ok(set(PR.well_id) == set(dev.well_id), "every development well has a profile")

    # 12. price deck
    need = horizon_end.year + int(cfg["production_tail_years"])
    c.ok(PD.year.min() <= plan_start.year and PD.year.max() >= need, "price deck covers horizon + production tail",
         f"{PD.year.min()}-{PD.year.max()} vs need {need}")

    # 13. phases & campaigns
    for wid, g2 in PH.groupby("well_id"):
        w = wells[wid]
        for col in ("duration_p10_days", "duration_p50_days", "duration_p90_days"):
            if abs(g2[col].sum() - w[col]) > 0.15:
                c.ok(False, "phase durations sum to well total", f"{wid} {col}")
                break
    c.ok(True, "phase durations sum to well total") if not any("phase durations" in e for e in c.errors) else None
    bad = [b.batch_campaign_id for b in BC.itertuples()
           if len({wells[x]["installation_id"] for x in b.well_ids.split(";")}) != 1]
    c.ok(not bad, "campaign wells share one installation", str(bad))
    phased = set(W[W.is_phased.astype(str) == "True"].well_id)
    c.ok(phased == set(PH.well_id), "is_phased matches well_phases.csv")
    c.ok(all(wells[x]["batch_campaign_id"] for x in phased), "phased wells belong to a campaign")

    # 14. winter tiers
    tiers = W.winter_tier.value_counts(normalize=True)
    if size != "small":
        bad = {t: round(tiers.get(t, 0), 3) for t, v in TIER_TARGET.items() if abs(tiers.get(t, 0) - v) > 0.05}
        c.ok(not bad, "winter-tier shares within ±5% of 35/45/20", str(bad))
    c.ok(set(W.winter_tier) <= set(TIER_TARGET), "winter_tier values valid")

    # 15. CAPEX
    tot = W.capex_rig_usd + W.capex_materials_usd + W.capex_other_usd
    c.ok((abs(tot - W.capex_usd) < 1).all(), "capex_usd = rig + materials + other")
    shr = W.capex_rig_usd / W.capex_usd
    no_tie = W.has_new_tie_in.astype(str) != "True"
    c.ok(((shr[no_tie] >= 0.40) & (shr[no_tie] <= 0.60)).all(), "rig share of CAPEX 40-60% (excl. new tie-ins)",
         str(shr[no_tie & ((shr < 0.4) | (shr > 0.6))].head().to_dict()))
    rdc = W.capex_rig_usd / pd.to_numeric(W.duration_p50_days)
    c.ok((abs(rdc - W.rig_day_cost_usd) <= 100).all(), "rig_day_cost_usd = capex_rig_usd / P50")

    # 16. strategic wells & edge cases
    strat = W[W.is_strategic.astype(str) == "True"]
    c.ok((strat.is_mandatory.astype(str) == "True").all(), "strategic wells are mandatory")
    c.ok(all(plan_start <= d(a) < d(b) <= horizon_end for a, b in zip(strat.strategic_window_start, strat.strategic_window_end)),
         "strategic windows inside horizon")
    present = {t for ts in W.test_tag for t in split_set(ts)}
    missing = [t for t in EXPECTED_TAGS if t not in present]
    if size == "small":
        if missing:
            c.warn(f"edge cases not present in small size: {missing}")
    else:
        c.ok(not missing, "all edge cases present", str(missing))
    return c


def validate_units(root: Path, cfg: dict, c: Check) -> Check:
    ref = root.parent / root.name.replace("_NOK_Sm3", "")
    c.ok(ref.exists(), "USD sibling dataset exists", str(ref))
    if not ref.exists():
        return c
    fx = float(cfg["fx_nok_per_usd"])
    a, b = pd.read_csv(ref / "wells.csv"), pd.read_csv(root / "wells.csv")
    for col in ["capex_usd", "capex_rig_usd", "rig_day_cost_usd", "discovery_value_usd", "opex_fixed_usd_per_year"]:
        nok = col.replace("_usd", "_nok")
        c.ok(np.allclose(a[col].fillna(0), b[nok].fillna(0) / fx), f"wells {col} converts back")
    c.ok(np.allclose(a.opex_variable_usd_per_boe, b.opex_variable_nok_per_sm3oe / fx / BOE_PER_SM3),
         "variable OPEX converts back")
    pa, pb = pd.read_csv(ref / "production_profiles.csv"), pd.read_csv(root / "production_profiles.csv")
    c.ok(np.allclose(pa.oil_bbl, pb.oil_sm3 * BOE_PER_SM3) and np.allclose(pa.gas_mmbtu, pb.gas_sm3 * MMBTU_PER_SM3_GAS),
         "production volumes convert back")
    da, db = pd.read_csv(ref / "price_deck.csv"), pd.read_csv(root / "price_deck.csv")
    # revenue invariance: volume x price identical in USD after conversion
    rev_usd = pa.oil_bbl.sum() * da.oil_usd_bbl.mean() + pa.gas_mmbtu.sum() * da.gas_usd_mmbtu.mean()
    rev_nok = pb.oil_sm3.sum() * db.oil_nok_sm3.mean() + pb.gas_sm3.sum() * db.gas_nok_sm3.mean()
    c.ok(abs(rev_nok / fx - rev_usd) / rev_usd < 1e-9, "revenue identical after unit conversion")
    for f in ["reference_schedule.csv", "frozen_schedule.csv", "well_predecessors.csv", "batch_campaigns.csv"]:
        c.ok(pd.read_csv(ref / f).equals(pd.read_csv(root / f)), f"{f} unchanged by unit conversion")
    return c


def main():
    roots = [Path(p) for p in sys.argv[1:]] or [Path(__file__).parent / "data" / "default"]
    failed = False
    for root in roots:
        c = validate(root)
        print(f"\n== {root.name}: {len(c.passed)} passed, {len(c.errors)} failed, {len(c.warnings)} warnings")
        for e in c.errors:
            print(f"  FAIL  {e}")
        for w in c.warnings:
            print(f"  WARN  {w}")
        failed |= bool(c.errors)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
