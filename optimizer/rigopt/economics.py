"""Well value (spec §8.1-8.3), pre-tax, internal USD.

Definitions (exact, also used by the independent checker):

* DF(t) = (1 + r) ** (-(t - valuation_day) / 365.25), t in days from plan start.
* Production month m = 1..T (T = production_tail_years*12) of a well completed on
  day E falls in calendar month  month(E) + m  (prices by that month's year) and
  is discounted at  t = E + 30.4375 * m.
* Revenue_m = oil_m * oil_price + gas_m * gas_price;  OPEX_m = opex_fixed/12 + opex_var * boe_m,
  boe = oil_bbl + gas_mmbtu / 5.8.  OPEX runs for all T months.
* NPV = sum_m (Revenue_m - OPEX_m) * DF(E + 30.4375 m)  -  sum_phase capex_phase * DF(start_phase)
* EMV = PoS * discovery_value * DF(E + 30.4375 * lag)  -  capex * DF(start)

Because DF(E + 30.4375 m) = DF(E) * (1+r)^(-m/12), the production part is
PV[month(E)] * DF(E) with PV precomputed per completion month.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import BOE_PER_SM3, MMBTU_PER_BOE, MMBTU_PER_SM3_GAS, Model


class Economics:
    def __init__(self, m: Model):
        self.m = m
        s = m.s
        self.r = s.discount_rate
        T = 12 * s.production_tail_years
        self.T = T
        last_day = max(r.ce for r in m.rigs) + 400
        self.n_months = (m.date(last_day).year - m.plan_start.year) * 12 + 13
        self.month_of_day = [(m.date(d).year - m.plan_start.year) * 12 + m.date(d).month - m.plan_start.month
                             for d in range(0, last_day + 1)]
        self.df_day = [(1 + self.r) ** (-d / 365.25) for d in range(-400, last_day + 400 + 40 * 31)]
        # monthly price arrays from plan start, flat after the last deck year
        P = m.price.set_index("year")
        n = self.n_months + T + 2
        y0 = m.plan_start.year
        mo0 = m.plan_start.month - 1
        years = [y0 + (mo0 + k) // 12 for k in range(n)]
        ylast = int(P.index.max())
        self.oil_p = np.array([float(P.loc[min(y, ylast)].oil_usd_bbl) for y in years])
        self.gas_p = np.array([float(P.loc[min(y, ylast)].gas_usd_mmbtu) for y in years])
        self.vm = (1 + self.r) ** (-np.arange(1, T + 1) / 12.0)

        prof = pd.read_csv(m.root / "production_profiles.csv")
        if m.input_unit_set == "NOK_Sm3":
            prof["oil_bbl"] = prof.pop("oil_sm3") * BOE_PER_SM3
            prof["gas_mmbtu"] = prof.pop("gas_sm3") * MMBTU_PER_SM3_GAS
        prof["oil_bbl"] = prof.oil_bbl.round(2)
        prof["gas_mmbtu"] = prof.gas_mmbtu.round(2)
        self.profiles = {}
        for wid, g in prof.groupby("well_id"):
            oil = np.zeros(T)
            gas = np.zeros(T)
            idx = g.month_index.values.astype(int)
            k = idx <= T
            oil[idx[k] - 1] = g.oil_bbl.values[k]
            gas[idx[k] - 1] = g.gas_mmbtu.values[k]
            self.profiles[wid] = (oil, gas)

        W = m.wells_df.set_index("well_id")
        self.pv = {}  # well idx -> list PV per completion month (discounted to completion)
        for w in m.wells:
            if w.is_expl:
                continue
            oil, gas = self.profiles.get(w.well_id, (np.zeros(T), np.zeros(T)))
            boe = oil + gas / MMBTU_PER_BOE
            row = W.loc[w.well_id]
            opex = float(row.opex_fixed_usd_per_year) / 12.0 + float(row.opex_variable_usd_per_boe) * boe
            seg_o = self.oil_p[1:self.n_months + T]
            seg_g = self.gas_p[1:self.n_months + T]
            rev = np.correlate(seg_o, oil * self.vm, "valid") + np.correlate(seg_g, gas * self.vm, "valid")
            self.pv[w.idx] = list(rev - float(np.dot(opex, self.vm)))
        # capex split per phase
        self.capex_split = {}
        for w in m.wells:
            if w.phased:
                self.capex_split[w.idx] = {ph: w.capex * p["capex_share"] for ph, p in w.phases.items()}
            else:
                self.capex_split[w.idx] = {"full": w.capex}

    def df(self, t: float) -> float:
        if float(t).is_integer():
            return self.df_day[int(t) + 400]
        return (1 + self.r) ** (-t / 365.25)

    def value(self, wi: int, seg_starts: dict, end: int) -> float:
        """seg_starts: phase -> start day; end: completion day of the last phase."""
        w = self.m.wells[wi]
        cap = sum(self.capex_split[wi][ph] * self.df_day[s + 400] for ph, s in seg_starts.items())
        if w.is_expl:
            return w.pos * w.disc_value * self.df(end + 30.4375 * w.disc_lag) - cap
        c = self.month_of_day[end] if end < len(self.month_of_day) else len(self.pv[wi]) - 1
        pv = self.pv[wi]
        c = min(c, len(pv) - 1)
        return pv[c] * self.df_day[end + 400] - cap

    def value_if_ideal(self, wi: int, start: int) -> float:
        """Value if drilled from `start` at base duration (no weather), used for explanations."""
        w = self.m.wells[wi]
        d = int(np.ceil(w.base))
        if w.phased:
            th = int(np.ceil(w.phases["top_hole"]["base"]))
            return self.value(wi, {"top_hole": start, "main": start + th}, start + d)
        return self.value(wi, {"full": start}, start + d)

    def yearly_cashflows(self, wi: int, seg_starts: dict, end: int) -> dict:
        """Undiscounted yearly revenue / opex / capex / oil / gas for reports (internal units)."""
        m = self.m
        w = m.wells[wi]
        out = {}

        def add(y, k, v):
            out.setdefault(y, dict(revenue=0.0, opex=0.0, capex=0.0, oil_bbl=0.0, gas_mmbtu=0.0))[k] += v

        for ph, s in seg_starts.items():
            add(m.date(s).year, "capex", self.capex_split[wi][ph])
        if w.is_expl:
            return out
        oil, gas = self.profiles.get(w.well_id, (np.zeros(self.T), np.zeros(self.T)))
        row = m.wells_df.set_index("well_id").loc[w.well_id] if not hasattr(self, "_W") else self._W.loc[w.well_id]
        c0 = self.month_of_day[end]
        y0, mo0 = m.plan_start.year, m.plan_start.month - 1
        for k in range(self.T):
            mi = c0 + k + 1
            y = y0 + (mo0 + mi) // 12
            boe = oil[k] + gas[k] / MMBTU_PER_BOE
            add(y, "revenue", oil[k] * self.oil_p[mi] + gas[k] * self.gas_p[mi])
            add(y, "opex", float(row.opex_fixed_usd_per_year) / 12 + float(row.opex_variable_usd_per_boe) * boe)
            add(y, "oil_bbl", oil[k])
            add(y, "gas_mmbtu", gas[k])
        return out

    def prepare_reports(self):
        self._W = self.m.wells_df.set_index("well_id")
