"""Turn a decoded solution into the outputs of spec §9-10 (JSON-able dicts, output unit set)."""
from __future__ import annotations

import math
import random
from collections import defaultdict

import numpy as np

from .data import BOE_PER_SM3, MMBTU_PER_BOE, MMBTU_PER_SM3_GAS
from .decoder import Problem, Result, decode


class Units:
    def __init__(self, P: Problem):
        self.set = P.m.s.unit_set
        self.fx = P.m.fx_valuation
        nok = self.set == "NOK_Sm3"
        self.money_unit = "NOK" if nok else "USD"
        self.oil_unit = "Sm³" if nok else "bbl"
        self.gas_unit = "Sm³" if nok else "MMBtu"
        self.boe_unit = "Sm³ o.e." if nok else "boe"
        self._nok = nok

    def money(self, usd):
        return usd * self.fx if self._nok else usd

    def oil(self, bbl):
        return bbl / BOE_PER_SM3 if self._nok else bbl

    def gas(self, mmbtu):
        return mmbtu / MMBTU_PER_SM3_GAS if self._nok else mmbtu

    def boe(self, boe):
        return boe / BOE_PER_SM3 if self._nok else boe


def season_of(d) -> int:
    """Winter season label: Nov-Dec of year y and Jan-Feb of y+1 belong to season y."""
    return d.year if d.month >= 7 else d.year - 1


class Analysis:
    def __init__(self, P: Problem, seqs, res: Result, name: str = "optimized", ref_res: Result | None = None):
        self.P, self.seqs, self.res, self.name = P, seqs, res, name
        self.m, self.e = P.m, P.e
        self.u = Units(P)
        self.ref_res = ref_res
        self.e.prepare_reports()

    # ------------------------------------------------------------------ schedule rows
    def schedule_rows(self):
        m, u, res = self.m, self.u, self.res
        rows = []
        locked = set(m.s.locked_wells)
        for wi, segd in res.well_seg.items():
            w = m.wells[wi]
            for ph, (ri, a, b, base, sail, frozen) in segd.items():
                wdays = sum(1 for d in range(a, b) if m.is_winter(d))
                rows.append(dict(
                    well_id=w.well_id, name=w.name, phase=ph, rig_id=m.rigs[ri].rig_id, rig_name=m.rigs[ri].name,
                    asset=w.asset, field_id=w.field_id, installation_id=w.installation_id, category=w.category,
                    purpose=w.purpose, winter_tier=w.winter_tier if hasattr(w, "winter_tier") else w.tier,
                    start_date=m.date(a).isoformat(), end_date=m.date(b).isoformat(), start=a, end=b,
                    base_duration_days=round(base, 1), effective_duration_days=b - a,
                    weather_added_days=round((b - a) - base, 1), sail_days_in=sail, is_frozen=bool(frozen),
                    locked_by="admin (frozen)" if frozen else ("planner" if w.well_id in locked else ""),
                    locked_at=m.plan_start.isoformat() if frozen else "", winter_days=wdays,
                    wow_cost=u.money(w.rig_day_cost * max(0.0, (b - a) - base)),
                    value=u.money(res.well_value.get(wi, 0.0)) if ph in ("full", "main") else 0.0))
        rows.sort(key=lambda r: (r["rig_id"], r["start"]))
        return rows

    # ------------------------------------------------------------------ economics
    def economics(self):
        m, e, u, res = self.m, self.e, self.u, self.res
        per_well = []
        yearly = defaultdict(lambda: dict(revenue=0.0, opex=0.0, capex=0.0, oil=0.0, gas=0.0, boe=0.0))
        by = {k: defaultdict(float) for k in ("asset", "field", "rig", "category")}
        for wi, v in res.well_value.items():
            w = m.wells[wi]
            segd = res.well_seg[wi]
            starts = {ph: g[1] for ph, g in segd.items()}
            end = max(g[2] for g in segd.values())
            rig = segd.get("main", segd.get("full"))[0]
            cf = e.yearly_cashflows(wi, starts, end)
            for y, d in cf.items():
                Y = yearly[y]
                Y["revenue"] += d["revenue"]; Y["opex"] += d["opex"]; Y["capex"] += d["capex"]
                Y["oil"] += d["oil_bbl"]; Y["gas"] += d["gas_mmbtu"]; Y["boe"] += d["oil_bbl"] + d["gas_mmbtu"] / MMBTU_PER_BOE
            by["asset"][w.asset] += v
            by["field"][self.field_name(w)] += v
            by["rig"][m.rigs[rig].rig_id] += v
            by["category"][w.category] += v
            per_well.append(dict(well_id=w.well_id, asset=w.asset, field=self.field_name(w),
                                 category=w.category, purpose=w.purpose, rig_id=m.rigs[rig].rig_id,
                                 start_date=m.date(min(starts.values())).isoformat(), end_date=m.date(end).isoformat(),
                                 value=u.money(v), capex=u.money(w.capex),
                                 frozen=all(g[5] for g in segd.values())))
        ys = sorted(y for y in yearly if y >= m.plan_start.year)
        yearly_rows = [dict(year=y, revenue=u.money(yearly[y]["revenue"]), opex=u.money(yearly[y]["opex"]),
                            capex=u.money(yearly[y]["capex"]), oil=u.oil(yearly[y]["oil"]),
                            gas=u.gas(yearly[y]["gas"]), boe=u.boe(yearly[y]["boe"])) for y in ys]
        dev = sum(v for wi, v in res.well_value.items() if not m.wells[wi].is_expl)
        emv = sum(v for wi, v in res.well_value.items() if m.wells[wi].is_expl)
        return dict(total=u.money(dev + emv), npv_development=u.money(dev), emv_exploration=u.money(emv),
                    per_well=per_well, yearly=yearly_rows,
                    by_asset={k: u.money(v) for k, v in sorted(by["asset"].items())},
                    by_field={k: u.money(v) for k, v in sorted(by["field"].items())},
                    by_rig={k: u.money(v) for k, v in sorted(by["rig"].items())},
                    by_category={k: u.money(v) for k, v in sorted(by["category"].items())})

    def field_name(self, w):
        f = self.m.fields.get(w.field_id)
        return f["name"] if f else ("Exploration" if w.is_expl else str(w.field_id))

    # ------------------------------------------------------------------ utilization
    def utilization(self):
        m, res = self.m, self.res
        out = []
        H0, H1 = 0, m.H
        for r in m.rigs:
            lo, hi = max(H0, r.cs), min(H1, r.ce)
            span = max(0, hi - lo)
            drill = wx = sail = 0.0
            for wi, segd in res.well_seg.items():
                for ph, (ri, a, b, base, sv, fr) in segd.items():
                    if ri != r.idx:
                        continue
                    a2, b2 = max(a, lo), min(b, hi)
                    if b2 <= a2:
                        continue
                    frac = (b2 - a2) / (b - a)
                    drill += base * frac
                    wx += ((b - a) - base) * frac
                    s0, s1 = max(a - sv, lo), min(a, hi)
                    sail += max(0, s1 - s0)
            un = sum(max(0, min(b, hi) - max(a, lo)) for a, b, _ in r.unavail)
            idle = max(0.0, span - drill - wx - sail - un)
            out.append(dict(rig_id=r.rig_id, name=r.name, rig_type=r.rig_type, available_days=span,
                            drilling_days=round(drill, 1), weather_added_days=round(wx, 1), sailing_days=round(sail, 1),
                            idle_days=round(idle, 1), unavailable_days=un,
                            utilization=round((drill + wx) / span, 3) if span else 0.0,
                            contract_end=m.date(r.ce).isoformat()))
        return out

    # ------------------------------------------------------------------ exploration
    def exploration(self):
        m, u, res = self.m, self.u, self.res
        rows = []
        for y in sorted(set(m.targets) | set(res.expl_count)):
            lo, hi = m.targets.get(y, (None, None))
            n = res.expl_count.get(y, 0)
            st = "n/a" if lo is None else ("below min" if n < lo else ("above max" if n > hi else
                                                                      ("at min (binding)" if n == lo else "ok")))
            rows.append(dict(year=y, spudded=n, min=lo, max=hi, status=st))
        strat = []
        for w in m.wells:
            if not w.strategic:
                continue
            segd = res.well_seg.get(w.idx)
            if segd:
                a = min(g[1] for g in segd.values()); b = max(g[2] for g in segd.values())
                ok = w.sw is None or (a >= w.sw[0] and b <= w.sw[1])
            strat.append(dict(well_id=w.well_id, prospect=getattr(w._row, "prospect_name", ""),
                              window_start=m.date(w.sw[0]).isoformat() if w.sw else "",
                              window_end=m.date(w.sw[1]).isoformat() if w.sw else "",
                              start_date=m.date(a).isoformat() if segd else "",
                              end_date=m.date(b).isoformat() if segd else "",
                              status=("in window" if ok else "OUTSIDE WINDOW") if segd else "NOT SCHEDULED"))
        risked = sum(v for wi, v in res.well_value.items() if m.wells[wi].is_expl)
        return dict(per_year=rows, strategic=strat, risked_value=u.money(risked),
                    n_wells=sum(1 for wi in res.well_value if m.wells[wi].is_expl))

    # ------------------------------------------------------------------ explanations
    def unscheduled(self):
        P, m, e, res = self.P, self.m, self.e, self.res
        scheduled = set(res.well_value)
        sched_or_frozen = scheduled | set(P.frozen_wells_done)
        busy = defaultdict(list)
        for wi, segd in res.well_seg.items():
            for ph, g in segd.items():
                busy[m.wells[wi].inst_key].append((g[1], g[2]))
        out = []
        for w in m.wells:
            if w.idx in scheduled or w.status in ("cancelled", "completed"):
                continue
            tid = P.first_task_of_well.get(w.idx)
            t = P.tasks[tid] if tid is not None else None
            reason, detail = None, ""
            ideal = e.value_if_ideal(w.idx, max(w.es, m.freeze_day))
            if not w.compat:
                reason, detail = "no_compatible_rig", self._why_incompatible(w)
            elif t is not None and not t.compat:
                reason, detail = "no_compatible_rig", "no rig left after optimization-freedom limits (rig changes not allowed)"
            elif t is not None and not t.insertable:
                reason, detail = "freedom_limit", "adding wells is not allowed at this optimization-freedom level"
            if reason is None:
                es = max(w.es, m.freeze_day, w.sw[0] if w.sw else 0)
                dl = min([x for x in (w.mcb, w.sw[1] if w.sw else None) if x is not None] or [10 ** 9])
                last_ce = max(m.rig_by_id[r].ce for r in w.compat)
                if (w.ls is not None and w.ls < es) or dl < es + w.base or last_ce < es + w.base or es >= m.H:
                    reason = "time_window_infeasible"
                    detail = (f"window {m.date(es)} .. {m.date(min(dl, m.H)) if dl < 10**9 else m.date(m.H)} "
                              f"cannot fit {w.base:.0f} days on a compatible rig")
            if reason is None:
                blocked = [m.wells[p].well_id for p, _ in w.preds if p not in sched_or_frozen]
                if blocked:
                    pv = sum(e.value_if_ideal(m.well_by_id[b].idx, max(m.well_by_id[b].es, m.freeze_day)) for b in blocked)
                    reason = "precedence_blocked"
                    tot = ideal + pv
                    detail = (f"predecessor(s) not scheduled: {', '.join(blocked)}; value at earliest start: this well "
                              f"{self.u.money(ideal)/1e6:,.1f} M, predecessor(s) {self.u.money(pv)/1e6:,.1f} M "
                              f"{self.u.money_unit}; " + ("together negative" if tot < 0 else
                              "together positive, but rig capacity is used for higher-value wells"))
            if reason is None and ideal < 0 and not w.mandatory:
                reason = "negative_value"
                detail = f"value if drilled at earliest start {self.u.money(ideal)/1e6:,.1f} M {self.u.money_unit}"
            if reason is None:
                es = max(w.es, m.freeze_day)
                ls = min([x for x in (w.ls, (w.mcb - math.ceil(w.base)) if w.mcb else None) if x is not None] or [m.H])
                if ls - es < 400:
                    iv = sorted(busy.get(w.inst_key, []))
                    t0, covered = es, True
                    for a, b in iv:
                        if a > t0:
                            break
                        t0 = max(t0, b)
                    covered = t0 > ls
                    if covered and iv:
                        reason = "installation_conflict"
                        detail = "installation occupied by another rig throughout the well's time window"
            if reason is None:
                reason = "no_capacity_in_horizon"
                detail = (f"compatible rigs ({', '.join(sorted(w.compat))}) are used for higher-value wells; "
                          f"value if drilled at earliest start {self.u.money(ideal)/1e6:,.1f} M {self.u.money_unit}")
            if t is not None and t.tid in res.dropped and reason == "no_capacity_in_horizon":
                reason = res.dropped[t.tid]
            out.append(dict(well_id=w.well_id, asset=w.asset, category=w.category, purpose=w.purpose,
                            mandatory=w.mandatory, reason=reason, detail=detail,
                            ideal_value=self.u.money(ideal), compatible_rigs=";".join(sorted(w.compat)),
                            test_tag=w.test_tag))
        return out

    def _why_incompatible(self, w):
        """Name the first rule (spec §5.1) that excludes each rig, then summarise."""
        m = self.m
        R = m.wells_df  # noqa: F841
        import pandas as pd
        rigs_df = pd.read_csv(m.root / "rigs.csv").set_index("rig_id")
        why = defaultdict(list)
        for r in m.rigs:
            x = rigs_df.loc[r.rig_id]
            if not (x.min_water_depth_m <= w.water_depth_m <= x.max_water_depth_m):
                why[f"water depth {w.water_depth_m:.0f} m outside rig range"].append(r.rig_id)
            elif w.measured_depth_m > x.max_drilling_depth_m:
                why[f"MD {w.measured_depth_m:.0f} m above rig max"].append(r.rig_id)
            elif not set(w.required_capabilities) <= r.capabilities:
                miss = sorted(set(w.required_capabilities) - r.capabilities)
                why["missing " + "+".join(miss)].append(r.rig_id)
            else:
                why["rig type / allowed / excluded override"].append(r.rig_id)
        return "; ".join(f"{k}: {', '.join(v)}" for k, v in why.items())

    def infeasibility(self, unscheduled):
        m, res, P = self.m, self.res, self.P
        items = []
        for x in unscheduled:
            if x["mandatory"]:
                items.append(f"Mandatory/strategic well {x['well_id']} could not be scheduled ({x['reason']}: {x['detail']}).")
        for r in self.exploration()["per_year"]:
            if r["status"] in ("below min", "above max"):
                items.append(f"Exploration target {r['year']}: {r['spudded']} wells spudded, target {r['min']}-{r['max']}.")
        if res.parts["incomplete_campaign_wells"]:
            items.append(f"{res.parts['incomplete_campaign_wells']} campaign wells have a top hole without main phase.")
        if res.parts.get("freedom_penalty"):
            items.append(f"Changed wells ({res.parts['changed_tasks']}) exceed max_wells_changed ({m.s.max_wells_changed}).")
        for t in P.tasks:
            if t.required and t.tid not in res.placed and t.ref is not None and not m.s.allow_add_remove_wells:
                items.append(f"Reference task {t.key} could not be kept within the freedom limits.")
        return items

    # ------------------------------------------------------------------ bottlenecks
    def bottlenecks(self):
        m, res = self.m, self.res
        out = []
        for cap in ("HPHT", "WELL_TEST"):
            rigs = [r for r in m.rigs if cap in r.capabilities]
            need = [w for w in m.wells if cap in w.required_capabilities and w.compat]
            days = sum(max(0, min(r.ce, m.H) - max(r.cs, m.freeze_day)) for r in rigs)
            used = sum(g[2] - g[1] for wi, segd in res.well_seg.items() for g in segd.values()
                       if m.rigs[g[0]] in rigs and g[1] >= m.freeze_day)
            demand = sum(w.base for w in need if w.idx not in P_frozen(self.P))
            uns = [w.well_id for w in need if w.idx not in res.well_value]
            out.append(f"{cap}: {len(rigs)} rig(s) ({', '.join(r.rig_id for r in rigs)}) with {days:,} rig-days after the "
                       f"frozen period; {used:,} used. Demand {demand:,.0f} base days from {len(need)} wells; "
                       f"{len(uns)} not scheduled.")
        for r in self.exploration()["per_year"]:
            if r["status"].startswith("at min"):
                out.append(f"Exploration target {r['year']} binds: exactly the minimum {r['min']} wells are spudded.")
        for r in m.rigs:
            if r.ce < m.H:
                out.append(f"{r.rig_id} {r.name}: contract ends {m.date(r.ce)} inside the horizon "
                           f"(extension option not modelled).")
        # late-horizon idle capacity
        idle = defaultdict(float)
        for row in self.yearly_rig_activity():
            idle[row["year"]] += row["idle"]
        late = [y for y, v in sorted(idle.items()) if v > 0.35 * 365 * len(m.rigs)]
        if late:
            out.append(f"Portfolio thins out: more than a third of fleet capacity is idle in {', '.join(map(str, late))}.")
        return out

    def yearly_rig_activity(self):
        m, res = self.m, self.res
        out = []
        for y in range(m.plan_start.year, m.horizon_end.year):
            a0, a1 = m.day(f"{y}-01-01"), m.day(f"{y + 1}-01-01")
            for r in m.rigs:
                lo, hi = max(a0, r.cs), min(a1, r.ce)
                span = max(0, hi - lo)
                busy = 0
                for wi, segd in res.well_seg.items():
                    for g in segd.values():
                        if g[0] == r.idx:
                            busy += max(0, min(g[2], hi) - max(g[1] - g[4], lo))
                un = sum(max(0, min(b, hi) - max(a, lo)) for a, b, _ in r.unavail)
                out.append(dict(year=y, rig_id=r.rig_id, busy=busy, idle=max(0, span - busy - un)))
        return out

    # ------------------------------------------------------------------ weather
    def weather(self):
        m, res, u = self.m, self.res, self.u
        seasons = defaultdict(lambda: dict(wells={"none": [], "avoid": [], "strongly_avoid": []},
                                           weather_added_days=0.0, penalty=0.0))
        for wi, segd in res.well_seg.items():
            w = m.wells[wi]
            for ph, (ri, a, b, base, sv, fr) in segd.items():
                wd = [d for d in range(a, b) if m.is_winter(d)]
                if not wd:
                    continue
                sea = season_of(m.date(wd[len(wd) // 2]))
                S = seasons[sea]
                share = len(wd) / max(1, b - a)
                if share >= 0.5:
                    S["wells"][w.tier].append(dict(well_id=w.well_id, phase=ph, rig_id=m.rigs[ri].rig_id,
                                                   winter_share=round(share, 2), frozen=fr))
                added = (b - a) - base
                S["weather_added_days"] += added
                S["penalty"] += m.s.winter_penalty_share[w.tier] * w.rig_day_cost * max(0.0, added)
                S["wow_cost"] = S.get("wow_cost", 0.0) + w.rig_day_cost * max(0.0, added)
        out = []
        for sea in sorted(seasons):
            S = seasons[sea]
            out.append(dict(season=f"{sea}/{str(sea + 1)[-2:]}", wells=S["wells"],
                            counts={k: len(v) for k, v in S["wells"].items()},
                            weather_added_days=round(S["weather_added_days"], 1), penalty=u.money(S["penalty"]),
                            wow_cost=u.money(S.get("wow_cost", 0.0))))
        return out

    # ------------------------------------------------------------------ batch campaigns
    def campaigns(self):
        m, res = self.m, self.res
        out = []
        for cid, c in m.campaigns.items():
            ths = [(m.wells[i], res.well_seg.get(i, {}).get("top_hole")) for i in c["wells"]]
            if not all(g for _, g in ths):
                out.append(dict(campaign=cid, installation=c["installation_id"], status="not scheduled", wells=[]))
                continue
            rig = m.rigs[ths[0][1][0]].rig_id
            wells = []
            for w, th in ths:
                mn = res.well_seg[w.idx].get("main")
                wells.append(dict(well_id=w.well_id, top_hole_start=m.date(th[1]).isoformat(),
                                  top_hole_end=m.date(th[2]).isoformat(),
                                  main_rig=m.rigs[mn[0]].rig_id if mn else "",
                                  main_start=m.date(mn[1]).isoformat() if mn else "",
                                  main_end=m.date(mn[2]).isoformat() if mn else "",
                                  gap_days=(mn[1] - th[2]) if mn else None,
                                  same_rig=bool(mn and m.rigs[mn[0]].rig_id == rig)))
            out.append(dict(campaign=cid, installation=c["installation_id"], rig_id=rig, status="scheduled",
                            start=m.date(min(g[1] for _, g in ths)).isoformat(),
                            end=m.date(max(g[2] for _, g in ths)).isoformat(), wells=wells))
        return out

    # ------------------------------------------------------------------ discovery risk (spec §6.2)
    def discovery_risk(self):
        P, m, u, res = self.P, self.m, self.u, self.res
        base = decode(P, self.seqs, relax=True)
        starts0 = {wi: min(g[1] for g in sd.values()) for wi, sd in base.well_seg.items()}
        out = []
        for tid, (ri, segs) in sorted(res.placed.items(), key=lambda x: x[1][1][0][2]):
            t = P.tasks[tid]
            if t.kind != "W":
                continue
            w = m.wells[t.wells[0]]
            if not w.is_expl or not w.disc_dur:
                continue
            r2 = decode(P, self.seqs, dur_override={(w.idx, "full"): w.disc_dur}, relax=True)
            delayed = []
            for wi, sd in r2.well_seg.items():
                if wi == w.idx or wi not in starts0:
                    continue
                d = min(g[1] for g in sd.values()) - starts0[wi]
                if d > 0:
                    delayed.append((d, m.wells[wi].well_id, m.rigs[sd[min(sd, key=lambda k: sd[k][1])][0]].rig_id))
            delayed.sort(key=lambda x: (-x[0], x[1]))
            lost = sum(base.well_value.values()) - sum(r2.well_value.values()) + (
                r2.well_value.get(w.idx, 0) - base.well_value.get(w.idx, 0))
            out.append(dict(well_id=w.well_id, rig_id=m.rigs[ri].rig_id, start_date=m.date(segs[0][2]).isoformat(),
                            extra_days=round(w.disc_dur - w.base, 1), n_delayed=len(delayed),
                            max_delay_days=delayed[0][0] if delayed else 0,
                            delayed=[dict(well_id=x[1], rig_id=x[2], days=x[0]) for x in delayed[:12]],
                            value_impact_other_wells=-u.money(lost)))
        return out

    # ------------------------------------------------------------------ Monte Carlo (spec §10)
    def monte_carlo(self, n: int = 200, seed: int = 7):
        P, m, u = self.P, self.m, self.u
        rng = random.Random(seed)
        segs = [(wi, ph) for tid in self.res.placed for (wi, ph, *_ ) in self.res.placed[tid][1]]

        def q(p10, p50, p90, x):
            lo = max(0.5 * p10, p10 - (p50 - p10) * 0.25)
            hi = p90 + (p90 - p50) * 0.25
            pts = [(0.0, lo), (0.1, p10), (0.5, p50), (0.9, p90), (1.0, hi)]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                if x <= x1:
                    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
            return hi

        starts = defaultdict(list)
        values = []
        for k in range(n):
            ov = {}
            for (wi, ph) in segs:
                w = m.wells[wi]
                if ph == "full":
                    p10, p50, p90 = w.p10, w.p50, w.p90
                else:
                    p = w.phases[ph]; p10, p50, p90 = p["p10"], p["p50"], p["p90"]
                ov[(wi, ph)] = q(p10, p50, p90, rng.random())
            ws = {r: math.exp(rng.gauss(0, 0.25)) for r in m.wow_month}
            r = decode(P, self.seqs, dur_override=ov, relax=True, wow_scale=ws)
            values.append(sum(r.well_value.values()))
            for wi, sd in r.well_seg.items():
                if all(g[5] for g in sd.values()):
                    continue
                starts[wi].append(min(g[1] for g in sd.values()))
        pct = lambda a, p: float(np.percentile(a, p))
        wells = []
        for wi, lst in starts.items():
            wells.append(dict(well_id=m.wells[wi].well_id,
                              planned=m.date(min(g[1] for g in self.res.well_seg[wi].values())).isoformat()
                              if wi in self.res.well_seg else "",
                              p10=m.date(round(pct(lst, 10))).isoformat(), p50=m.date(round(pct(lst, 50))).isoformat(),
                              p90=m.date(round(pct(lst, 90))).isoformat(), n=len(lst)))
        wells.sort(key=lambda x: x["planned"])
        return dict(samples=n, value_p10=u.money(pct(values, 10)), value_p50=u.money(pct(values, 50)),
                    value_p90=u.money(pct(values, 90)), value_mean=u.money(float(np.mean(values))),
                    deterministic=u.money(sum(self.res.well_value.values())), wells=wells,
                    note="Fixed order per rig; durations sampled from P10/P50/P90 (piecewise-linear), "
                         "weather factor scaled per region by a lognormal (sigma 0.25). Wells are never dropped; "
                         "time windows may be violated in samples.")

    # ------------------------------------------------------------------ comparison (spec §9)
    def compare(self, other: Result, other_name: str = "reference"):
        m, u, res = self.m, self.u, self.res
        a_wells, b_wells = set(res.well_value), set(other.well_value)
        moved, rigchg, rows = [], [], []
        for wi in sorted(a_wells | b_wells, key=lambda i: m.wells[i].well_id):
            w = m.wells[wi]
            A, B = res.well_seg.get(wi), other.well_seg.get(wi)
            if A and all(g[5] for g in A.values()):
                continue
            if A and wi in a_wells and B and wi in b_wells:
                sa = min(g[1] for g in A.values()); sb = min(g[1] for g in B.values())
                ra = m.rigs[A[min(A, key=lambda k: A[k][1])][0]].rig_id
                rb = m.rigs[B[min(B, key=lambda k: B[k][1])][0]].rig_id
                if sa != sb or ra != rb:
                    rows.append(dict(well_id=w.well_id, change="moved" if ra == rb else "rig change",
                                     from_rig=rb, to_rig=ra, from_start=m.date(sb).isoformat(),
                                     to_start=m.date(sa).isoformat(), shift_days=sa - sb,
                                     value_delta=u.money(res.well_value[wi] - other.well_value[wi])))
            elif wi in a_wells:
                sa = min(g[1] for g in A.values())
                ra = m.rigs[A[min(A, key=lambda k: A[k][1])][0]].rig_id
                rows.append(dict(well_id=w.well_id, change="added", from_rig="", to_rig=ra, from_start="",
                                 to_start=m.date(sa).isoformat(), shift_days=None,
                                 value_delta=u.money(res.well_value[wi])))
            elif wi in b_wells:
                sb = min(g[1] for g in B.values())
                rb = m.rigs[B[min(B, key=lambda k: B[k][1])][0]].rig_id
                rows.append(dict(well_id=w.well_id, change="dropped", from_rig=rb, to_rig="",
                                 from_start=m.date(sb).isoformat(), to_start="", shift_days=None,
                                 value_delta=-u.money(other.well_value[wi])))
        cnt = defaultdict(int)
        for r in rows:
            cnt[r["change"]] += 1
        shifts = [abs(r["shift_days"]) for r in rows if r["shift_days"] is not None]
        return dict(against=other_name, value_delta=u.money(sum(res.well_value.values()) - sum(other.well_value.values())),
                    objective_delta=u.money(res.objective - other.objective), counts=dict(cnt),
                    mean_abs_shift_days=round(float(np.mean(shifts)), 1) if shifts else 0.0,
                    max_abs_shift_days=max(shifts) if shifts else 0, rows=rows)

    # ------------------------------------------------------------------ KPIs
    def kpis(self, unscheduled):
        m, u, res = self.m, self.u, self.res
        parts = res.parts
        return dict(
            name=self.name, unit_set=m.s.unit_set, money_unit=u.money_unit,
            portfolio_value=u.money(parts["value"]), objective=u.money(res.objective),
            winter_penalty=u.money(parts["winter_penalty"]), batch_penalty=u.money(parts["batch_penalty"]),
            wells_scheduled=len(res.well_value),
            wells_frozen=sum(1 for wi, sd in res.well_seg.items() if all(g[5] for g in sd.values())),
            wells_unscheduled=len(unscheduled), sail_days=parts["sail_days"], idle_days=parts["idle_days"],
            exploration_target_deviation=parts["target_deviation"], missing_mandatory=parts["missing_required"],
            changed_vs_reference=parts["changed_tasks"], optimization_freedom=m.s.optimization_freedom,
            frozen_months=m.s.frozen_months, plan_start=m.plan_start.isoformat(),
            label=getattr(self, "label", self.name), description=getattr(self, "description", ""),
            rig_speed_knots=m.s.rig_speed_knots,
            same_installation_move_days=m.s.same_installation_move_days, mob_in_sailing=m.s.mob_in_sailing,
            sail_rounding=m.s.sail_rounding,
            wow_cost=u.money(sum(m.wells[wi].rig_day_cost * max(0.0, (g[2] - g[1]) - g[3])
                                 for wi, sd in res.well_seg.items() for g in sd.values())),
            n_exploration=sum(1 for wi in res.well_value if m.wells[wi].is_expl),
            n_strategic=sum(1 for w in m.wells if w.strategic),
            n_strategic_in_window=sum(1 for w in m.wells if w.strategic and w.idx in res.well_value),
            target_years=len(m.targets),
            target_years_met=sum(1 for y, (lo, hi) in m.targets.items() if lo <= res.expl_count.get(y, 0) <= hi),
            unscheduled_reasons={r: sum(1 for x in unscheduled if x["reason"] == r)
                                 for r in sorted({x["reason"] for x in unscheduled})},
            freeze_end=m.freeze_end.isoformat(), horizon_end=m.horizon_end.isoformat())

    def full(self, with_mc=True, with_discovery=True, mc_samples=200):
        uns = self.unscheduled()
        out = dict(kpis=self.kpis(uns), schedule=self.schedule_rows(), economics=self.economics(),
                   utilization=self.utilization(), yearly_rig_activity=self.yearly_rig_activity(),
                   exploration=self.exploration(), unscheduled=uns, infeasibility=self.infeasibility(uns),
                   bottlenecks=self.bottlenecks(), weather=self.weather(), campaigns=self.campaigns())
        if self.ref_res is not None:
            out["comparison"] = self.compare(self.ref_res)
        if with_discovery:
            out["discovery_risk"] = self.discovery_risk()
        if with_mc:
            out["monte_carlo"] = self.monte_carlo(mc_samples)
        return out


def P_frozen(P: Problem):
    return set(P.frozen_wells_done)
