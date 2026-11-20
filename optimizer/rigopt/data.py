"""Load a synthetic dataset folder into an internal model.

Internal units are always USD, bbl (oil) and MMBtu (gas).  A NOK_Sm3 dataset is
converted on load with the price-deck FX rate and the standard factors below;
results are converted back on output (see ``units``).

Time is an integer day index relative to ``plan_start``; an assignment occupies
[start, end).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

BOE_PER_SM3 = 6.2898
MMBTU_PER_SM3_GAS = 0.0353
MMBTU_PER_BOE = 5.8
TIER_FACTOR_DEFAULT = {"none": 1.0, "avoid": 1.5, "strongly_avoid": 3.5}
PENALTY_SHARE_DEFAULT = {"none": 0.0, "avoid": 0.10, "strongly_avoid": 0.50}
REGIONS = ["north_sea_south", "north_sea_north", "norwegian_sea", "barents_sea"]
DAY_OFFSET = 400  # calendar arrays start this many days before plan_start

# Optimization-freedom presets (spec §8.5).  None = unlimited.
FREEDOM_PRESETS = {
    "minimal": dict(max_shift_days=30, allow_rig_change=False, allow_add_remove_wells=False,
                    max_wells_changed=15),
    "moderate": dict(max_shift_days=120, allow_rig_change=True, allow_add_remove_wells=True,
                     max_wells_changed=None),
    "free": dict(max_shift_days=None, allow_rig_change=True, allow_add_remove_wells=True,
                 max_wells_changed=None),
}


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or str(v).strip() == ""


def _set(v) -> frozenset:
    return frozenset() if _blank(v) else frozenset(str(v).split(";"))


def _bool(v) -> bool:
    return str(v).strip().lower() == "true"


def read_cfg(path: Path) -> dict:
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            cfg[k.strip()] = v.strip()
    return cfg


@dataclass
class Settings:
    """Run settings: dataset config.yaml overridden by CLI/run options."""
    frozen_months: int = 12
    horizon_years: int = 10
    duration_percentile: str = "P50"
    discount_rate: float = 0.08
    production_tail_years: int = 20
    same_installation_move_days: int | None = 0   # None = take config.yaml (spec default 1); 0 = same location
    mob_in_sailing: bool = False              # rig down/up is part of each well's duration, not of the move
    sail_rounding: str = "nearest"            # nearest | up  (whole days)
    unit_set: str = "USD_bbl_MMBtu"          # output unit set
    tier_factor: dict = field(default_factory=lambda: dict(TIER_FACTOR_DEFAULT))
    winter_penalty_share: dict = field(default_factory=lambda: dict(PENALTY_SHARE_DEFAULT))
    winter_months: tuple = (11, 12, 1, 2)
    batch_same_rig_bonus: float = 2.0e6       # USD per main phase on the campaign rig
    batch_gap_penalty_per_day: float = 20.0e3  # USD per day between top hole and main phase
    sail_day_weight: float = 10.0e3           # tie-break 1 (USD per sailing day)
    idle_day_weight: float = 1.0e3            # tie-break 2 (USD per idle rig day)
    # optimization freedom (spec §8.5)
    optimization_freedom: str = "free"
    max_shift_days: int | None = None
    allow_rig_change: bool = True
    allow_add_remove_wells: bool = True
    max_wells_changed: int | None = None
    change_tolerance_days: int = 14           # a well counts as "changed" if it moved more than this
    change_penalty_weight: float = 0.0        # USD per day moved (soft); rig change counts as 30 days
    locked_wells: tuple = ()
    n_variants: int = 1
    price_case: str = "base"                  # base | low | high
    rig_speed_knots: float | None = 7.0       # transit speed used for every rig (None = per-rig value from rigs.csv)
    # search
    seed: int = 42
    iterations: int | None = None             # None = size-dependent default
    mc_samples: int = 200

    def apply_freedom(self, level: str | None = None):
        level = level or self.optimization_freedom
        if level in FREEDOM_PRESETS:
            self.optimization_freedom = level
            for k, v in FREEDOM_PRESETS[level].items():
                setattr(self, k, v)
        return self


@dataclass
class Well:
    idx: int
    well_id: str
    name: str
    asset: str
    field_id: str
    installation_id: str
    inst_key: str          # installation id, or "@lat,lon" for wells without an installation
    region: str
    lat: float
    lon: float
    water_depth_m: float
    measured_depth_m: float
    category: str
    purpose: str
    fluid: str
    is_expl: bool
    is_hpht: bool
    tier: str
    sens: float
    base: float            # scheduled base duration (selected percentile)
    p10: float
    p50: float
    p90: float
    disc_dur: float | None
    phased: bool
    phases: dict           # phase -> dict(base, p10, p50, p90, sens, capex_share, min_lag)
    campaign: str | None
    es: int                # earliest start (day)
    ls: int | None         # latest start
    mcb: int | None        # must complete by (end <= mcb)
    sw: tuple | None       # strategic window (start, end)
    mandatory: bool
    strategic: bool
    capex: float
    rig_day_cost: float
    pos: float
    disc_value: float
    disc_lag: int
    status: str
    compat: frozenset      # compatible rig ids (spec §5.1)
    preds: list            # [(pred well idx, lag)]
    test_tag: str


@dataclass
class Rig:
    idx: int
    rig_id: str
    name: str
    rig_type: str
    capabilities: frozenset
    knots: float
    mob: float
    move_sens: float
    cs: int
    ce: int
    unavail: list           # [(start, end, reason)]
    init_well: str
    init_lat: float
    init_lon: float


class Model:
    """Everything the optimizer needs, in internal units."""

    def __init__(self, root: Path, settings: Settings | None = None):
        self.root = Path(root)
        cfg = read_cfg(self.root / "config.yaml")
        self.cfg = cfg
        self.input_unit_set = cfg.get("unit_set", "USD_bbl_MMBtu")
        s = settings or Settings()
        self.s = s
        if settings is None:
            s.frozen_months = int(cfg.get("frozen_months", 12))
            s.unit_set = self.input_unit_set
        s.horizon_years = int(cfg.get("horizon_years", s.horizon_years))
        s.duration_percentile = cfg.get("duration_percentile", s.duration_percentile)
        s.discount_rate = float(cfg.get("discount_rate", s.discount_rate))
        s.production_tail_years = int(cfg.get("production_tail_years", s.production_tail_years))
        if s.same_installation_move_days is None:
            s.same_installation_move_days = int(cfg.get("same_installation_move_days", 1))
        for t in ("none", "avoid", "strongly_avoid"):
            if f"tier_factor_{t}" in cfg:
                s.tier_factor[t] = float(cfg[f"tier_factor_{t}"])
            if f"winter_penalty_share_{t}" in cfg:
                s.winter_penalty_share[t] = float(cfg[f"winter_penalty_share_{t}"])
        if "winter_months" in cfg:
            s.winter_months = tuple(int(x) for x in cfg["winter_months"].split(","))

        self.plan_start = date.fromisoformat(cfg["plan_start"])
        self.horizon_end = _add_months(self.plan_start, 12 * s.horizon_years)
        self.freeze_end = _add_months(self.plan_start, s.frozen_months)
        self.H = self.day(self.horizon_end)
        self.freeze_day = self.day(self.freeze_end)
        self._load()

    # ------------------------------------------------------------------ time helpers
    def day(self, d) -> int:
        if isinstance(d, str):
            d = date.fromisoformat(d)
        return (d - self.plan_start).days

    def date(self, day: int) -> date:
        return self.plan_start + timedelta(days=int(day))

    def _opt_day(self, v):
        return None if _blank(v) else self.day(str(v))

    # ------------------------------------------------------------------ loading
    def _load(self):
        root, s = self.root, self.s
        nok = self.input_unit_set == "NOK_Sm3"
        rd = lambda f: pd.read_csv(root / f, keep_default_na=False, na_values=[""])
        price_file = {"base": "price_deck.csv", "low": "price_deck_low.csv",
                      "high": "price_deck_high.csv"}[s.price_case]
        P = rd(price_file).sort_values("year")
        if nok:
            P["oil_usd_bbl"] = P.oil_nok_sm3 / P.fx_nok_usd / BOE_PER_SM3
            P["gas_usd_mmbtu"] = P.gas_nok_sm3 / P.fx_nok_usd / MMBTU_PER_SM3_GAS
        P["oil_usd_bbl"] = P.oil_usd_bbl.round(6)
        P["gas_usd_mmbtu"] = P.gas_usd_mmbtu.round(6)
        self.price = P[["year", "oil_usd_bbl", "gas_usd_mmbtu", "fx_nok_usd"]].reset_index(drop=True)
        self.fx = {int(r.year): float(r.fx_nok_usd) for r in self.price.itertuples()}
        fx0 = self.fx.get(self.plan_start.year, float(self.price.fx_nok_usd.iloc[0]))
        self.fx_valuation = fx0

        W = rd("wells.csv")
        if nok:  # convert to USD (inverse of the generator's conversion)
            fxw = fx0
            for c in ["capex", "capex_rig", "capex_materials", "capex_other", "rig_day_cost",
                      "discovery_value", "value_at_plan_start"]:
                W[c + "_usd"] = W.pop(c + "_nok") / fxw
            W["opex_fixed_usd_per_year"] = W.pop("opex_fixed_nok_per_year") / fxw
            W["opex_variable_usd_per_boe"] = W.pop("opex_variable_nok_per_sm3oe") / fxw / BOE_PER_SM3
        # round money so both unit sets give bit-identical internal inputs
        for c in ["capex_usd", "rig_day_cost_usd", "discovery_value_usd", "opex_fixed_usd_per_year"]:
            W[c] = W[c].map(lambda v: v if _blank(v) else round(float(v), 0))
        W["opex_variable_usd_per_boe"] = W["opex_variable_usd_per_boe"].map(
            lambda v: v if _blank(v) else round(float(v), 6))
        self.wells_df = W

        I = rd("installations.csv")
        self.installations = {r.installation_id: r._asdict() for r in I.itertuples(index=False)}
        F = rd("fields.csv")
        self.fields = {r.field_id: r._asdict() for r in F.itertuples(index=False)}
        R = rd("rigs.csv")
        U = rd("rig_unavailability.csv")
        PH = rd("well_phases.csv")
        PR = rd("well_predecessors.csv")
        BC = rd("batch_campaigns.csv")
        T = rd("exploration_targets.csv")
        WX = rd("weather.csv")

        # weather calendar
        self.wow_month = {r: [0.0] * 12 for r in REGIONS}
        for x in WX.itertuples():
            self.wow_month[x.region][int(x.month) - 1] = float(x.wow_factor)
        self.n_days = self.day(_add_months(self.plan_start, 12 * (s.horizon_years + 4))) + 1200
        n = self.n_days + DAY_OFFSET
        self.month_of_year = [(self.plan_start + timedelta(days=d - DAY_OFFSET)).month for d in range(n)]
        self.wow_day = {r: [self.wow_month[r][m - 1] for m in self.month_of_year] for r in REGIONS}
        self.wow_cum = {r: [0.0] + list(np.cumsum(self.wow_day[r])) for r in REGIONS}
        self.region_idx = {r: i for i, r in enumerate(REGIONS)}

        # rigs
        self.rigs: list[Rig] = []
        for i, r in enumerate(R.itertuples(index=False)):
            un = [(self.day(u.start), self.day(u.end), u.reason) for u in U.itertuples() if u.rig_id == r.rig_id]
            self.rigs.append(Rig(i, r.rig_id, r.name, r.rig_type, _set(r.capabilities),
                                 float(s.rig_speed_knots) if s.rig_speed_knots else float(r.transit_speed_knots),
                                 float(r.mob_demob_days), float(r.move_sensitivity), self.day(r.contract_start),
                                 self.day(r.contract_end), sorted(un), "" if _blank(r.initial_well_id) else r.initial_well_id,
                                 float(r.initial_lat), float(r.initial_lon)))
        self.rig_by_id = {r.rig_id: r for r in self.rigs}

        # phases
        phases = {}
        pkey = {"P10": "duration_p10_days", "P50": "duration_p50_days", "P90": "duration_p90_days"}[s.duration_percentile]
        for p in PH.itertuples(index=False):
            phases.setdefault(p.well_id, {})[p.phase] = dict(
                base=float(getattr(p, pkey)), p10=float(p.duration_p10_days), p50=float(p.duration_p50_days),
                p90=float(p.duration_p90_days), sens=float(p.weather_sensitivity), capex_share=float(p.capex_share),
                min_lag=int(p.min_lag_days))

        # wells
        self.wells: list[Well] = []
        self.well_by_id: dict[str, Well] = {}
        for i, w in enumerate(W.itertuples(index=False)):
            inst = "" if _blank(w.installation_id) else w.installation_id
            is_expl = w.category in ("exploration", "appraisal")
            base = float(getattr(w, pkey))
            sw = None
            if _bool(w.is_strategic) and not _blank(w.strategic_window_start):
                sw = (self.day(w.strategic_window_start), self.day(w.strategic_window_end))
            wl = Well(
                idx=i, well_id=w.well_id, name=w.name, asset="Exploration" if _blank(w.asset) else w.asset,
                field_id="" if _blank(w.field_id) else w.field_id, installation_id=inst,
                inst_key=inst if inst else f"@{float(w.lat):.5f},{float(w.lon):.5f}", region=w.region,
                lat=float(w.lat), lon=float(w.lon), water_depth_m=float(w.water_depth_m),
                measured_depth_m=float(w.measured_depth_m), category=w.category, purpose=w.purpose, fluid=w.fluid,
                is_expl=is_expl, is_hpht=_bool(w.is_hpht), tier=w.winter_tier, sens=float(w.weather_sensitivity),
                base=base, p10=float(w.duration_p10_days), p50=float(w.duration_p50_days),
                p90=float(w.duration_p90_days),
                disc_dur=None if _blank(w.discovery_duration_p50_days) else float(w.discovery_duration_p50_days),
                phased=_bool(w.is_phased) and w.well_id in phases, phases=phases.get(w.well_id, {}),
                campaign=None if _blank(w.batch_campaign_id) else w.batch_campaign_id,
                es=self.day(w.earliest_start) if not _blank(w.earliest_start) else 0,
                ls=self._opt_day(w.latest_start), mcb=self._opt_day(w.must_complete_by), sw=sw,
                mandatory=_bool(w.is_mandatory) or _bool(w.is_strategic), strategic=_bool(w.is_strategic),
                capex=float(w.capex_usd), rig_day_cost=float(w.rig_day_cost_usd),
                pos=0.0 if _blank(w.probability_of_success) else float(w.probability_of_success),
                disc_value=0.0 if _blank(w.discovery_value_usd) else float(w.discovery_value_usd),
                disc_lag=0 if _blank(w.discovery_value_lag_months) else int(float(w.discovery_value_lag_months)),
                status=w.status, compat=frozenset(), preds=[],
                test_tag="" if _blank(w.test_tag) else str(w.test_tag))
            wl._row = w
            self.wells.append(wl)
            self.well_by_id[wl.well_id] = wl
            if wl.phased:  # phase durations are a split of the whole-well duration
                wl.base = sum(p["base"] for p in wl.phases.values())

        # compatibility (spec §5.1) - recomputed from attributes, not read from the matrix
        for w in self.wells:
            row = w._row
            req = set(_set(row.required_capabilities))
            if w.is_hpht:
                req.add("HPHT")
            if w.is_expl:
                req |= {"WELL_TEST", "FORMATION_TEST"}
            types, allowed, excluded = _set(row.allowed_rig_types), _set(row.allowed_rigs), _set(row.excluded_rigs)
            ok = []
            for r in self.rigs:
                if not (r_min(R, r) <= w.water_depth_m <= r_max(R, r)):
                    continue
                if w.measured_depth_m > float(R.iloc[r.idx].max_drilling_depth_m):
                    continue
                if not req <= r.capabilities:
                    continue
                if types and r.rig_type not in types:
                    continue
                if allowed and r.rig_id not in allowed:
                    continue
                if r.rig_id in excluded:
                    continue
                ok.append(r.rig_id)
            w.compat = frozenset(ok)
            w.required_capabilities = frozenset(req)

        for p in PR.itertuples(index=False):
            self.well_by_id[p.well_id].preds.append((self.well_by_id[p.predecessor_id].idx, int(p.min_lag_days)))

        self.campaigns = {}
        for b in BC.itertuples(index=False):
            ids = str(b.well_ids).split(";")
            self.campaigns[b.batch_campaign_id] = dict(id=b.batch_campaign_id, installation_id=b.installation_id,
                                                       wells=[self.well_by_id[x].idx for x in ids])
        self.targets = {int(t.year): (int(t.min_wells), int(t.max_wells)) for t in T.itertuples()}

        # schedules
        self.reference_df = self._read_schedule(root / "reference_schedule.csv")
        frozen_file = root / "frozen_schedule.csv"
        if int(self.cfg.get("frozen_months", 12)) == s.frozen_months and frozen_file.exists():
            self.frozen_df = self._read_schedule(frozen_file)
        else:  # derive from the last approved schedule for a different freeze length
            ref = self.reference_df
            starts = ref.groupby("well_id").s.transform("min")
            camp_start = {}
            for cid, c in self.campaigns.items():
                ids = {self.wells[i].well_id for i in c["wells"]}
                th = ref[ref.well_id.isin(ids) & (ref.phase == "top_hole")]
                if len(th):
                    camp_start[cid] = th.s.min()
            keep = []
            for k, a in ref.iterrows():
                w = self.well_by_id[a.well_id]
                if a.phase == "top_hole" and w.campaign in camp_start:
                    keep.append(camp_start[w.campaign] < self.freeze_day)
                else:
                    keep.append(a.s < self.freeze_day)
            self.frozen_df = ref[keep].copy()
            self.frozen_df["is_frozen"] = True
            del starts

        # sail distance cache
        self._nm = {}

    def _read_schedule(self, path: Path) -> pd.DataFrame:
        S = pd.read_csv(path, keep_default_na=False, na_values=[""])
        S["s"] = S.start_date.map(self.day)
        S["e"] = S.end_date.map(self.day)
        return S

    # ------------------------------------------------------------------ physics shared by decoder & reports
    def mean_wow(self, region: str, start: int, length: int) -> float:
        a = start + DAY_OFFSET
        b = a + max(1, length)
        c = self.wow_cum[region]
        return (c[b] - c[a]) / (b - a)

    def eff_duration(self, base: float, sens: float, tier: str, region: str, start: int) -> int:
        m = self.mean_wow(region, start, int(math.ceil(base)))
        return int(math.ceil(base * (1.0 + sens * self.s.tier_factor[tier] * m)))

    def loc_of_well(self, w: Well) -> tuple:
        return (w.well_id, w.installation_id or None, w.lat, w.lon, w.region)

    def initial_loc(self, r: Rig) -> tuple:
        if r.init_well and r.init_well in self.well_by_id:
            return self.loc_of_well(self.well_by_id[r.init_well])
        return (None, None, r.init_lat, r.init_lon, "north_sea_north")

    def nm(self, a: tuple, b: tuple) -> float:
        key = (a[2], a[3], b[2], b[3])
        v = self._nm.get(key)
        if v is None:
            v = great_circle_nm(a[2], a[3], b[2], b[3])
            self._nm[key] = v
        return v

    def sail_days(self, r: Rig, frm: tuple | None, to: tuple, depart: int) -> int:
        """Days between two wells on one rig (see README 'Sailing').

        same well (next phase) -> 0; same installation -> same_installation_move_days;
        otherwise transit = nm / (knots * 24) [+ mob/demob if mob_in_sailing], times
        (1 + move_sensitivity * wow_factor(destination, departure day)), rounded to whole days."""
        if frm is not None and frm[0] is not None and frm[0] == to[0]:
            return 0
        if frm is not None and frm[1] and to[1] and frm[1] == to[1]:
            return int(self.s.same_installation_move_days)
        base = self.nm(frm, to) / (r.knots * 24.0) + (r.mob if self.s.mob_in_sailing else 0.0)
        days = base * (1.0 + r.move_sens * self.wow_day[to[4]][depart + DAY_OFFSET])
        if self.s.sail_rounding == "up":
            return int(math.ceil(days - 1e-9))
        return int(math.floor(days + 0.5))

    def is_winter(self, day: int) -> bool:
        return self.month_of_year[day + DAY_OFFSET] in self.s.winter_months


def r_min(R, r):
    return float(R.iloc[r.idx].min_water_depth_m)


def r_max(R, r):
    return float(R.iloc[r.idx].max_water_depth_m)


def great_circle_nm(lat1, lon1, lat2, lon2) -> float:
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(min(1.0, a)))


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1) if d.day == 1 else date(d.year + y, m + 1, min(d.day, 28))
