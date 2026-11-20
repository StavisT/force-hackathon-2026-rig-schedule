#!/usr/bin/env python3
"""Deterministic synthetic data generator for the rig schedule optimizer (spec v0.4).

Usage:
    python generate_data.py --size all --seed 42 --unit-set both
"""
from __future__ import annotations

import argparse
import heapq
import math
import shutil
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from rigsched_common import (
    BOE_PER_SM3, MMBTU_PER_BOE, MMBTU_PER_SM3_GAS, REGION_BOX, TIER_FACTOR,
    WINTER_PENALTY_SHARE, Calendar, compatible, effective_duration, great_circle_nm,
    sail_days,
)

PLAN_START = date(2027, 1, 1)
FROZEN_MONTHS = 12
DISCOUNT_RATE = 0.08
TAIL_YEARS = 20
PROFILE_MONTHS = TAIL_YEARS * 12
SAME_INST_MOVE = 1
FX = 10.5
DURATION_PERCENTILE = "P50"

SIZES = {
    "small": dict(n_rigs=3, n_dev=25, n_expl=5, horizon_years=3, n_campaigns=1),
    "default": dict(n_rigs=8, n_dev=250, n_expl=50, horizon_years=10, n_campaigns=4),
    "large": dict(n_rigs=20, n_dev=1000, n_expl=150, horizon_years=15, n_campaigns=5),
}

ASSET_SHARE = {"Valhall": 0.20, "Fenris": 0.05, "Alvheim": 0.18, "Ivar Aasen": 0.08,
               "Edvard Grieg": 0.10, "Skarv": 0.12, "Yggdrasil": 0.27}
ASSET_CODE = {"Valhall": "VAL", "Fenris": "FEN", "Alvheim": "ALV", "Ivar Aasen": "IVA",
              "Edvard Grieg": "EDG", "Skarv": "SKA", "Yggdrasil": "YGG"}
ASSET_REGION = {"Valhall": "north_sea_south", "Fenris": "north_sea_south",
                "Alvheim": "north_sea_north", "Ivar Aasen": "north_sea_north",
                "Edvard Grieg": "north_sea_north", "Skarv": "norwegian_sea",
                "Yggdrasil": "north_sea_north"}
ASSET_MD = {"Valhall": (4000, 7500), "Fenris": (5000, 6000), "Alvheim": (3000, 6000),
            "Ivar Aasen": (3000, 5000), "Edvard Grieg": (3000, 5500), "Skarv": (3500, 5500),
            "Yggdrasil": (3000, 6000)}
ASSET_QI = {"Valhall": (1500, 6000), "Fenris": (3000, 8000), "Alvheim": (2000, 8000),
            "Ivar Aasen": (1000, 5000), "Edvard Grieg": (1000, 5000), "Skarv": (1500, 6000),
            "Yggdrasil": (2000, 8000)}
ML_PROB = {"Valhall": 0.40, "Alvheim": 0.40, "Fenris": 0.0}

# asset, field, block, lat, lon, wd_min, wd_max, platforms, n_templates, fluid, oil_share
FIELDS = [
    ("Valhall", "Valhall", "2/8", 56.28, 3.39, 65, 75,
     ["Valhall Field Centre", "Valhall Flank North", "Valhall Flank South", "Valhall West Flank"], 0, "oil", 0.85),
    ("Valhall", "Hod", "2/11", 56.18, 3.45, 65, 75, ["Hod"], 0, "oil", 0.85),
    ("Fenris", "Fenris", "2/4", 56.60, 2.95, 65, 75, ["Fenris"], 0, "gas_condensate", 0.25),
    ("Alvheim", "Alvheim", "24/6", 59.57, 1.99, 120, 135, [], 3, "oil", 0.85),
    ("Alvheim", "Volund", "24/9", 59.45, 1.95, 115, 130, [], 2, "oil", 0.85),
    ("Alvheim", "Bøyla", "24/9", 59.35, 1.90, 115, 125, [], 2, "oil", 0.85),
    ("Alvheim", "Vilje", "25/4", 59.68, 2.20, 115, 125, [], 2, "oil", 0.85),
    ("Alvheim", "Skogul", "25/1", 59.80, 2.10, 110, 120, [], 2, "oil", 0.80),
    ("Alvheim", "Tyrving", "25/4", 59.62, 2.25, 115, 125, [], 2, "oil", 0.80),
    ("Ivar Aasen", "Ivar Aasen", "16/1", 58.92, 2.20, 105, 115, ["Ivar Aasen"], 0, "oil", 0.75),
    ("Edvard Grieg", "Edvard Grieg", "16/1", 58.84, 2.25, 105, 112, ["Edvard Grieg"], 0, "oil", 0.80),
    ("Edvard Grieg", "Solveig", "16/4", 58.75, 2.35, 105, 115, [], 2, "oil", 0.80),
    ("Skarv", "Skarv", "6507/5", 65.70, 7.60, 350, 450, [], 3, "gas", 0.30),
    ("Skarv", "Ærfugl", "6507/3", 65.80, 7.40, 350, 420, [], 3, "gas", 0.25),
    ("Skarv", "Idun", "6507/3", 65.90, 7.55, 370, 450, [], 2, "gas", 0.30),
    ("Yggdrasil", "Hugin", "25/2", 59.95, 2.40, 110, 125, ["Hugin A"], 0, "oil", 0.60),
    ("Yggdrasil", "Munin", "30/11", 60.05, 2.45, 115, 130, ["Munin"], 0, "oil", 0.60),
    ("Yggdrasil", "Fulla", "25/2", 59.85, 2.35, 110, 125, [], 2, "gas", 0.45),
    ("Yggdrasil", "Frøy", "25/2", 59.75, 2.55, 110, 125, [], 2, "oil", 0.65),
]
TEMPLATE_NAMES = {"Alvheim": ["Kameleon", "Kneler", "Boa"]}

RIG_TEMPLATES = [
    dict(rig_type="jackup", wd=(0, 150), caps=["HARSH_ENV", "HPHT"]),
    dict(rig_type="jackup", wd=(0, 150), caps=["HARSH_ENV", "WELL_TEST", "FORMATION_TEST"]),
    dict(rig_type="jackup", wd=(0, 120), caps=["HARSH_ENV"]),
    dict(rig_type="semisub", wd=(70, 1500), caps=["HARSH_ENV", "WINTERIZED", "WELL_TEST", "FORMATION_TEST", "HPHT"]),
    dict(rig_type="semisub", wd=(70, 1500), caps=["HARSH_ENV", "WINTERIZED", "HPHT"]),
    dict(rig_type="semisub", wd=(70, 1500), caps=["HARSH_ENV", "WINTERIZED"]),
    dict(rig_type="semisub", wd=(80, 500), caps=["HARSH_ENV"]),
    dict(rig_type="semisub", wd=(70, 1200), caps=["HARSH_ENV"]),
]
SMALL_RIG_TEMPLATES = [0, 3, 5]
BIRDS = ["Fulmar", "Gannet", "Kittiwake", "Puffin", "Guillemot", "Skua", "Tern", "Cormorant",
         "Eider", "Shearwater", "Razorbill", "Petrel", "Merganser", "Grebe", "Loon", "Plover",
         "Sandpiper", "Osprey", "Albatross", "Dunlin"]
PROSPECTS = ["Aurvandil", "Bestla", "Bylgja", "Dagr", "Eir", "Elli", "Fjalar", "Gefjun", "Gerd",
             "Gna", "Hlin", "Hermod", "Hoenir", "Hrimfaxi", "Hugi", "Jord", "Kvasir", "Lofn",
             "Magni", "Mani", "Modi", "Nanna", "Nott", "Ran", "Saga", "Sigyn", "Sjofn", "Skadi",
             "Sol", "Syn", "Thrud", "Vor", "Ymir", "Bil", "Hyrrokkin", "Gullveig", "Fenja",
             "Menja", "Hati", "Skoll", "Gjallar", "Brisingr", "Draupnir", "Eldhrimnir", "Fafnir",
             "Glasir", "Hreidmar", "Ivaldi", "Lit", "Mundilfari"]
PORTS = {"jackup": (58.97, 5.73), "semisub": (60.39, 5.32)}  # Stavanger / Bergen area
WOW_BASE = {  # Jun-Aug, Apr-May+Sep, Mar+Oct, Nov-Feb
    "north_sea_south": (0.03, 0.05, 0.08, 0.15),
    "north_sea_north": (0.04, 0.06, 0.10, 0.20),
    "norwegian_sea": (0.05, 0.08, 0.14, 0.28),
    "barents_sea": (0.05, 0.10, 0.20, 0.40),
}
MONTH_CLASS = {1: 3, 2: 3, 3: 2, 4: 1, 5: 1, 6: 0, 7: 0, 8: 0, 9: 1, 10: 2, 11: 3, 12: 3}


def add_months(d: date, n: int) -> date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def offset_km(lat, lon, km, bearing):
    dlat = km / 111.0 * math.cos(bearing)
    dlon = km / (111.0 * math.cos(math.radians(lat))) * math.sin(bearing)
    return lat + dlat, lon + dlon


def alloc(total: int, shares: dict) -> dict:
    raw = {k: total * v for k, v in shares.items()}
    out = {k: int(math.floor(v)) for k, v in raw.items()}
    rest = total - sum(out.values())
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[:rest]:
        out[k] += 1
    return out


def fmt(d):
    return "" if d is None else d.isoformat()


class Generator:
    def __init__(self, size: str, seed: int):
        self.size = size
        self.p = SIZES[size]
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.horizon_end = add_months(PLAN_START, 12 * self.p["horizon_years"])
        self.freeze_end = add_months(PLAN_START, FROZEN_MONTHS)
        self.H = (self.horizon_end - PLAN_START).days
        self.freeze_day = (self.freeze_end - PLAN_START).days
        self.wells: dict[str, dict] = {}
        self.tags: dict[str, list] = defaultdict(list)
        self.preds: list[tuple] = []
        self.campaigns: list[dict] = []
        self.phases: dict[str, list] = {}
        self.notes: list[str] = []

    # ------------------------------------------------------------------ helpers
    def u(self, a, b):
        return float(self.rng.uniform(a, b))

    def day(self, d: date) -> int:
        return (d - PLAN_START).days

    def random_month(self, a: date, b: date) -> date:
        n = (b.year - a.year) * 12 + b.month - a.month
        return add_months(a, int(self.rng.integers(0, max(1, n) + 1)))

    def tag(self, wid, t):
        if t not in self.tags[wid]:
            self.tags[wid].append(t)

    def untagged(self, pred=lambda w: True):
        return [w for w in self.wells.values() if not self.tags[w["well_id"]] and pred(w)]

    # ------------------------------------------------------------------ locations
    def build_locations(self):
        factor = self.p["n_dev"] / 250
        tmpl_mult = 2 if factor > 1.5 else 1
        self.assets, self.fields, self.insts = [], [], []
        for a in ASSET_SHARE:
            self.assets.append(dict(asset_id=ASSET_CODE[a], name=a, region=ASSET_REGION[a]))
        for (asset, fname, block, lat, lon, wmin, wmax, plats, ntmpl, fluid, oil_share) in FIELDS:
            fid = f"F-{ASSET_CODE[asset]}-{fname[:3].upper()}"
            fid = fid.replace("Ø", "O").replace("Æ", "AE")
            self.fields.append(dict(field_id=fid, asset_id=ASSET_CODE[asset], name=fname, block=block,
                                    hub_lat=lat, hub_lon=lon, wd_min=wmin, wd_max=wmax, fluid=fluid,
                                    oil_share=oil_share, region=ASSET_REGION[asset]))
            k = 0
            for i, pname in enumerate(plats):
                plat, plon = (lat, lon) if i == 0 else offset_km(lat, lon, self.u(2, 7), self.u(0, 2 * math.pi))
                k += 1
                self.insts.append(dict(installation_id=f"{fid}-P{k}", field_id=fid, asset_id=ASSET_CODE[asset],
                                       asset=asset, name=pname, type="platform", lat=round(plat, 5),
                                       lon=round(plon, 5), water_depth_m=round(self.u(wmin, wmax), 1)))
            names = TEMPLATE_NAMES.get(fname, [])
            for i in range(ntmpl * tmpl_mult):
                tname = names[i] if i < len(names) else f"{fname} T-{chr(65 + i)}"
                tlat, tlon = offset_km(lat, lon, self.u(1, 8), self.u(0, 2 * math.pi))
                k += 1
                self.insts.append(dict(installation_id=f"{fid}-T{k}", field_id=fid, asset_id=ASSET_CODE[asset],
                                       asset=asset, name=tname, type="subsea_template", lat=round(tlat, 5),
                                       lon=round(tlon, 5), water_depth_m=round(self.u(wmin, wmax), 1)))
        self.field_by_id = {f["field_id"]: f for f in self.fields}

    # ------------------------------------------------------------------ rigs
    def build_rigs(self):
        n = self.p["n_rigs"]
        idxs = SMALL_RIG_TEMPLATES if self.size == "small" else [i % 8 for i in range(n)]
        self.rigs = []
        jk = sm = 0
        for i, t in enumerate(idxs):
            rid = f"R{i + 1:02d}"
            if RIG_TEMPLATES[t]["rig_type"] == "jackup":
                name = f"Boreal {BIRDS[jk % len(BIRDS)]}"; jk += 1
            else:
                name = f"Polar {BIRDS[(sm + 7) % len(BIRDS)]}"; sm += 1
            tpl = RIG_TEMPLATES[t]
            jack = tpl["rig_type"] == "jackup"
            cs, ce, ext = date(2025, 1, 1), add_months(self.horizon_end, 12), ""
            if self.size != "small" and t == 6 and i < 8:
                cs = add_months(PLAN_START, 12)  # starts in year 2
            if self.size != "small" and t == 7:
                ce = date(PLAN_START.year + 5, 7, 1)  # ends in year 6
                ext = f"{ce.isoformat()}..{add_months(ce, 24).isoformat()} (option, not modelled)"
            self.rigs.append(dict(
                rig_id=rid, name=name, rig_type=tpl["rig_type"], template_index=t,
                min_water_depth_m=tpl["wd"][0], max_water_depth_m=tpl["wd"][1],
                max_drilling_depth_m=int(self.rng.integers(70, 91)) * 100,
                capabilities=set(tpl["caps"]),
                transit_speed_knots=round(self.u(3, 4) if jack else self.u(7, 9), 1),
                mob_demob_days=5 if jack else 3,
                move_sensitivity=round(self.u(0.8, 1.0) if jack else self.u(0.3, 0.5), 2),
                contract_start=cs, contract_end=ce, extension_option=ext,
                day_rate_usd=int(self.u(300, 380) if jack else self.u(380, 480)) * 1000,
                performance_factor=round(self.u(0.9, 1.1), 2),
            ))
        # unavailability: 1-2 yard stays / class surveys
        self.unavail = []
        for r in self.rigs:
            k = int(self.rng.integers(1, 3)) if self.p["horizon_years"] > 3 else 1
            for j in range(k):
                lo = add_months(PLAN_START, 12 + j * 60)
                hi = add_months(PLAN_START, min(12 * self.p["horizon_years"] - 2, 12 + j * 60 + 48))
                if hi <= lo:
                    hi = add_months(lo, 6)
                s = self.random_month(lo, hi) + timedelta(days=int(self.rng.integers(0, 28)))
                e = s + timedelta(days=int(self.rng.integers(20, 46)))
                self.unavail.append(dict(rig_id=r["rig_id"], start=s, end=e,
                                         reason="class survey" if j == 0 else "yard stay"))

    # ------------------------------------------------------------------ wells
    def build_dev_wells(self):
        per_asset = alloc(self.p["n_dev"], ASSET_SHARE)
        counter = defaultdict(int)
        self.used_insts = []
        for asset, n in per_asset.items():
            insts = [i for i in self.insts if i["asset"] == asset]
            if n < 2 * len(insts):
                k = max(1, n // 2)
                # keep a mix of platforms and templates where possible
                order = list(self.rng.permutation(len(insts)))
                insts = [insts[i] for i in sorted(order[:k])]
            k = len(insts)
            min_each = 2 if n >= 2 * k else 1
            w = self.rng.dirichlet(np.ones(k) * 3)
            counts = [min_each + e for e in self.rng.multinomial(n - min_each * k, w)]
            for inst, c in zip(insts, counts):
                if c == 0:
                    continue
                self.used_insts.append(inst)
                for j in range(c):
                    counter[asset] += 1
                    self.make_dev_well(inst, counter[asset], j)
        used = {i["installation_id"] for i in self.used_insts}
        self.insts = [i for i in self.insts if i["installation_id"] in used]
        self.inst_by_id = {i["installation_id"]: i for i in self.insts}

    def make_dev_well(self, inst, n, j):
        asset = inst["asset"]
        field = self.field_by_id[inst["field_id"]]
        wid = f"{ASSET_CODE[asset]}-{n:03d}"
        if inst["type"] == "platform":
            lat, lon = inst["lat"], inst["lon"]
        else:
            lat, lon = offset_km(inst["lat"], inst["lon"], self.u(0.01, 0.1), self.u(0, 2 * math.pi))
        wd = float(np.clip(inst["water_depth_m"] + self.u(-2, 2), field["wd_min"], field["wd_max"]))
        if asset == "Fenris":
            purpose = "producer"
        else:
            purpose = self.rng.choice(["producer", "water_injector", "gas_injector", "observation"],
                                      p=[0.70, 0.22, 0.05, 0.03])
        ml = self.rng.random() < ML_PROB.get(asset, 0.15) and purpose != "observation"
        hpht = asset == "Fenris" or (asset == "Skarv" and self.rng.random() < 0.15)
        md = self.u(*ASSET_MD[asset])
        tvd = md * (self.u(0.85, 0.95) if asset == "Fenris" else self.u(0.5, 0.85))
        fluid = field["fluid"] if purpose == "producer" else field["fluid"]
        earliest = PLAN_START
        if asset == "Yggdrasil":
            earliest = date(2027, 7, 1) if self.rng.random() < 0.6 else self.random_month(
                date(2027, 7, 1), add_months(PLAN_START, min(48, 12 * self.p["horizon_years"] - 12)))
        elif self.rng.random() < 0.2:
            earliest = self.random_month(PLAN_START, add_months(self.horizon_end, -24))
        must = None
        if asset != "Yggdrasil" and self.rng.random() < 0.10:
            must = add_months(earliest, int(self.rng.integers(24, 61)))
            if must > self.horizon_end:
                must = self.horizon_end
        self.wells[wid] = dict(
            well_id=wid, name=f"{field['block']}-{inst['name'][:1].upper()}-{j + 1}",
            asset=asset, field_id=field["field_id"], installation_id=inst["installation_id"],
            installation_type=inst["type"], region=field["region"], block=field["block"],
            lat=round(lat, 5), lon=round(lon, 5), water_depth_m=round(wd, 1),
            measured_depth_m=round(md), tvd_m=round(tvd), category="development", purpose=str(purpose),
            fluid=fluid, oil_share=field["oil_share"], is_hpht=bool(hpht), is_multilateral=bool(ml),
            n_laterals=int(self.rng.integers(2, 6)) if ml else 1,
            required_capabilities={"HPHT"} if hpht else set(), allowed_rig_types={"jackup"} if inst["type"] == "platform" else {"semisub"},
            allowed_rigs=set(), excluded_rigs=set(),
            earliest_start=earliest, latest_start=None, must_complete_by=must, is_mandatory=False,
            probability_of_success=None, discovery_value_usd=None, discovery_value_lag_months=None,
            is_strategic=False, strategic_window_start=None, strategic_window_end=None,
            prospect_name="", license="", batch_campaign_id="", value_boost=1.0, eur_override=None,
            p50_override=None, status="planned",
        )

    def build_expl_wells(self):
        n = self.p["n_expl"]
        hubs = [f for f in self.fields]
        n_strat = max(1, round(0.15 * n))
        strat_idx = set(self.rng.choice(n, n_strat, replace=False).tolist())
        for i in range(n):
            wid = f"EXP-{i + 1:03d}"
            r = self.rng.random()
            near = False
            if r < 0.6:
                if self.rng.random() < 0.7:
                    near = True
                    f = hubs[int(self.rng.integers(0, len(hubs)))]
                    while f["region"] == "norwegian_sea":
                        f = hubs[int(self.rng.integers(0, len(hubs)))]
                    lat, lon = offset_km(f["hub_lat"], f["hub_lon"], self.u(5, 30), self.u(0, 2 * math.pi))
                    wd = float(np.clip((f["wd_min"] + f["wd_max"]) / 2 + self.u(-20, 20), 65, 350))
                    block = f["block"]
                else:
                    lat, lon = self.u(56.2, 61.3), self.u(1.7, 3.9)
                    wd = self.u(65, 350)
                    q = self.rng.choice([2, 3, 7]) if lat < 57.5 else self.rng.choice([15, 16, 24, 25, 30, 31, 34, 35])
                    block = f"{q}/{int(self.rng.integers(1, 13))}"
                region = "north_sea_south" if lat < 57.5 else "north_sea_north"
            elif r < 0.9:
                lat, lon, wd, region = self.u(64.2, 66.8), self.u(5.2, 8.8), self.u(250, 1200), "norwegian_sea"
                block = f"{self.rng.choice([6506, 6507, 6406, 6608])}/{int(self.rng.integers(1, 13))}"
            else:
                lat, lon, wd, region = self.u(71.2, 72.8), self.u(18.5, 29.5), self.u(250, 500), "barents_sea"
                block = f"{self.rng.choice([7120, 7220, 7321, 7324])}/{int(self.rng.integers(1, 13))}"
            appraisal = self.rng.random() < 0.18
            hpht = self.rng.random() < 0.05
            strategic = i in strat_idx
            earliest, ws, we = PLAN_START, None, None
            if strategic:
                y_hi = min(6, self.p["horizon_years"])
                ws = self.random_month(add_months(PLAN_START, 12), add_months(PLAN_START, 12 * y_hi - 6))
                we = min(add_months(ws, int(self.rng.integers(6, 19))), self.horizon_end)
                earliest = ws
            else:
                earliest = self.random_month(PLAN_START, add_months(self.horizon_end, -12))
            pos = self.u(0.4, 0.7) if appraisal else self.u(0.10, 0.60)
            dv = float(np.clip(self.rng.lognormal(math.log(400e6), 0.8), 50e6, 1500e6))
            dv *= 0.6 if near else (1.5 if region in ("norwegian_sea", "barents_sea") else 1.0)
            dv = float(np.clip(dv, 50e6, 1500e6))
            caps = {"WELL_TEST", "FORMATION_TEST"} | ({"HPHT"} if hpht else set())
            pname = PROSPECTS[i % len(PROSPECTS)] + ("" if i < len(PROSPECTS) else f" {i // len(PROSPECTS) + 1}")
            self.wells[wid] = dict(
                well_id=wid, name=f"{block}-{int(self.rng.integers(1, 9))} {pname}",
                asset="", field_id="", installation_id="", installation_type="none", region=region, block=block,
                lat=round(lat, 5), lon=round(lon, 5), water_depth_m=round(wd, 1),
                measured_depth_m=round(self.u(2500, 5000)), tvd_m=0,
                category="appraisal" if appraisal else "exploration", purpose="n/a",
                fluid=str(self.rng.choice(["oil", "gas", "gas_condensate"], p=[0.5, 0.35, 0.15])),
                oil_share=0.0, is_hpht=bool(hpht), is_multilateral=False, n_laterals=1,
                required_capabilities=caps, allowed_rig_types=set(), allowed_rigs=set(), excluded_rigs=set(),
                earliest_start=earliest, latest_start=None, must_complete_by=None, is_mandatory=strategic,
                probability_of_success=round(pos, 3), discovery_value_usd=round(dv, -5),
                discovery_value_lag_months=int(self.rng.integers(24, 49)) if near else int(self.rng.integers(48, 73)),
                is_strategic=strategic, strategic_window_start=ws, strategic_window_end=we,
                prospect_name=pname, license=f"PL{int(self.rng.integers(1000, 1300))}", batch_campaign_id="",
                value_boost=1.0, eur_override=None, p50_override=None, status="planned", near_field=near,
            )
            self.wells[wid]["tvd_m"] = round(self.wells[wid]["measured_depth_m"] * self.u(0.9, 1.0))

    # ------------------------------------------------------------------ fixes & durations
    def compat_rigs(self, w):
        return [r["rig_id"] for r in self.rigs if compatible(r, w)]

    def fix_compatibility(self):
        test_rigs = [r for r in self.rigs if {"WELL_TEST"} <= r["capabilities"]]
        for w in self.wells.values():
            if "no_compatible_rig" in self.tags[w["well_id"]]:
                continue
            if self.compat_rigs(w):
                continue
            w["measured_depth_m"] = min(w["measured_depth_m"], 6800)
            w["tvd_m"] = min(w["tvd_m"], w["measured_depth_m"])
            if self.compat_rigs(w):
                continue
            if w["category"] != "development":
                need = w["required_capabilities"]
                cands = [r for r in test_rigs if need <= r["capabilities"]] or test_rigs
                r = max(cands, key=lambda r: r["max_water_depth_m"] - r["min_water_depth_m"])
                w["water_depth_m"] = float(np.clip(w["water_depth_m"], r["min_water_depth_m"], r["max_water_depth_m"]))
                if not self.compat_rigs(w) and "HPHT" in w["required_capabilities"] and not any(
                        need <= r["capabilities"] for r in self.rigs):
                    w["required_capabilities"].discard("HPHT"); w["is_hpht"] = False
            if not self.compat_rigs(w):
                raise RuntimeError(f"could not make {w['well_id']} compatible")

    def durations(self):
        for w in self.wells.values():
            if w["category"] == "development":
                core = w["measured_depth_m"] / 150 * (1 + 0.25 * (w["n_laterals"] - 1)) * (1.4 if w["is_hpht"] else 1)
                p50 = (15 + core) * self.u(0.9, 1.1) * (0.7 if w["purpose"] == "observation" else 1)
                p50 = float(np.clip(p50, 20, 150))
            else:
                p50 = self.u(25, 50) * (1.2 if w["is_hpht"] else 1.0)
            self.set_p(w, p50)
            if w["installation_type"] == "platform":
                w["weather_sensitivity"] = self.u(0.1, 0.3)
            elif w["installation_type"] == "subsea_template":
                w["weather_sensitivity"] = min(0.9, self.u(0.4, 0.7) + (0.1 if w["is_multilateral"] else 0))
            else:
                w["weather_sensitivity"] = self.u(0.5, 0.8)
            w["discovery_duration_p50_days"] = (round(p50 * self.u(1.6, 2.2), 1)
                                                if w["category"] != "development" else None)

    def set_p(self, w, p50):
        w["duration_p50_days"] = round(p50, 1)
        w["duration_p10_days"] = round(p50 * self.u(0.70, 0.85), 1)
        w["duration_p90_days"] = round(p50 * self.u(1.25, 1.60), 1)
        if w.get("discovery_duration_p50_days"):
            w["discovery_duration_p50_days"] = max(w["discovery_duration_p50_days"], round(p50 * 1.6, 1))

    # ------------------------------------------------------------------ edge cases
    def edge_geometry(self):
        nws = [w for w in self.wells.values() if w["category"] != "development"
               and w["region"] == "norwegian_sea" and not w["is_strategic"]]
        cands = nws or [w for w in self.wells.values() if w["category"] != "development" and not w["is_strategic"]]
        if cands:
            w = cands[0]
            w.update(region="norwegian_sea", lat=66.9, lon=4.6, water_depth_m=1800.0, block="6604/5")
            self.tag(w["well_id"], "no_compatible_rig")
        devs = [w for w in self.wells.values() if w["asset"] == "Skarv" and w["purpose"] == "producer"] or \
               [w for w in self.wells.values() if w["installation_type"] == "subsea_template"]
        if devs:
            devs[0]["required_capabilities"].add("MPD")
            self.tag(devs[0]["well_id"], "no_compatible_rig")

    def edge_attributes(self):
        W = self.wells
        for w in W.values():
            if w["asset"] == "Fenris" or (w["asset"] == "Skarv" and w["is_hpht"]):
                self.tag(w["well_id"], "hpht_bottleneck")
            if w["category"] != "development" and w["region"] in ("norwegian_sea", "barents_sea") \
                    and "no_compatible_rig" not in self.tags[w["well_id"]]:
                self.tag(w["well_id"], "test_rig_bottleneck")
        sub = lambda w: w["installation_type"] == "subsea_template"
        # installation_conflict
        alv = [i for i in self.insts if i["asset"] == "Alvheim" and i["type"] == "subsea_template"]
        for inst in alv:
            ws = self.untagged(lambda w: w["installation_id"] == inst["installation_id"] and w["purpose"] == "producer")
            if len(ws) >= 3:
                a, b = ws[0], ws[1]
                start = add_months(self.freeze_end, 3)
                dl = start + timedelta(days=int((a["duration_p50_days"] + b["duration_p50_days"]) * 1.6 + 30))
                for w in (a, b):
                    w.update(earliest_start=start, must_complete_by=dl, value_boost=1.8)
                    self.tag(w["well_id"], "installation_conflict")
                break
        # same_asset_parallel: two Skarv-area templates in different fields
        sk = [i for i in self.insts if i["asset"] == "Skarv"]
        by_field = {}
        for i in sk:
            by_field.setdefault(i["field_id"], i)
        if len(by_field) >= 2:
            for inst in list(by_field.values())[:2]:
                for w in self.untagged(lambda w: w["installation_id"] == inst["installation_id"]
                                       and w["purpose"] == "producer")[:3]:
                    w.update(value_boost=1.5, earliest_start=self.freeze_end)
                    self.tag(w["well_id"], "same_asset_parallel")
        # strategic_window: Barents, tight summer window
        bar = [w for w in W.values() if w["region"] == "barents_sea" and not self.tags[w["well_id"]] == ["no_compatible_rig"]]
        cand = [w for w in W.values() if w["category"] != "development" and not w["is_strategic"]
                and "no_compatible_rig" not in self.tags[w["well_id"]]]
        w = bar[0] if bar else (cand[0] if cand else None)
        if w is not None:
            yr = min(PLAN_START.year + 2, self.horizon_end.year - 1)
            w.update(region="barents_sea", lat=72.1, lon=24.3, water_depth_m=380.0, block="7220/7",
                     is_strategic=True, is_mandatory=True, strategic_window_start=date(yr, 5, 1),
                     strategic_window_end=date(yr, 10, 1), earliest_start=date(yr, 5, 1))
            self.tags[w["well_id"]] = [t for t in self.tags[w["well_id"]] if t != "test_rig_bottleneck"]
            self.tag(w["well_id"], "strategic_window")
        # winter_sensitive: ~90-day Alvheim multilateral + 3 more
        alv_w = self.untagged(lambda w: w["asset"] == "Alvheim" and sub(w) and w["purpose"] == "producer")
        if alv_w:
            w = alv_w[0]
            w.update(is_multilateral=True, n_laterals=4, weather_sensitivity=0.8, value_boost=2.0,
                     measured_depth_m=max(w["measured_depth_m"], 5500))
            self.set_p(w, 90.0)
            w["tier_override"] = "strongly_avoid"
            self.tag(w["well_id"], "winter_sensitive")
        for w in self.untagged(lambda w: sub(w) and w["purpose"] == "producer" and w["asset"] in ("Alvheim", "Skarv", "Yggdrasil"))[:3]:
            w.update(weather_sensitivity=0.75, value_boost=1.5, tier_override="strongly_avoid")
            if w["duration_p50_days"] < 60:
                self.set_p(w, self.u(60, 80))
            self.tag(w["well_id"], "winter_sensitive")
        # winter_filler: simple subsea wells that are fine in winter
        for w in self.untagged(lambda w: sub(w) and w["asset"] == "Alvheim" and not w["is_multilateral"])[:3]:
            self.set_p(w, self.u(70, 90))
            w.update(weather_sensitivity=0.15, tier_override="none")
            self.tag(w["well_id"], "winter_filler")
        # precedence_chain: Edvard Grieg platform <-> Solveig templates
        eg = self.untagged(lambda w: w["asset"] == "Edvard Grieg" and w["installation_type"] == "platform")
        so = self.untagged(lambda w: w["asset"] == "Edvard Grieg" and sub(w))
        if len(eg) >= 2 and len(so) >= 2:
            chain = [eg[0], so[0], eg[1], so[1]]
            for a, b in zip(chain, chain[1:]):
                self.preds.append((b["well_id"], a["well_id"], int(self.rng.integers(0, 31))))
            for w in chain:
                w["must_complete_by"] = None
                self.tag(w["well_id"], "precedence_chain")
        # negative_value
        for w in self.untagged(lambda w: w["category"] == "development" and w["purpose"] == "producer")[:5]:
            w["eur_override"] = self.u(0.10e6, 0.20e6)
            self.tag(w["well_id"], "negative_value")
        for w in self.untagged(lambda w: w["category"] != "development" and not w["is_strategic"])[:5 if self.size != "small" else 1]:
            w.update(probability_of_success=0.10, discovery_value_usd=round(self.u(40e6, 60e6), -5))
            self.tag(w["well_id"], "negative_value")
        # rig_contract_end
        ending = [r for r in self.rigs if r["contract_end"] < self.horizon_end]
        if ending:
            r = ending[0]
            cands = self.untagged(lambda w: sub(w) and compatible(r, w) and w["asset"] != "Yggdrasil")[:3]
            for w in cands:
                w.update(allowed_rigs={r["rig_id"]}, earliest_start=add_months(r["contract_end"], -18),
                         must_complete_by=None)
                self.tag(w["well_id"], "rig_contract_end")

    # ------------------------------------------------------------------ tiers, capex, profiles
    def tiers(self):
        order = ["none", "avoid", "strongly_avoid"]
        for w in self.wells.values():
            if w["category"] != "development":
                t = "strongly_avoid" if w["region"] in ("norwegian_sea", "barents_sea") else "avoid"
            elif w["installation_type"] == "subsea_template":
                t = "strongly_avoid" if (w["is_multilateral"] or w["duration_p50_days"] > 55 or w["is_hpht"]) else "avoid"
            else:
                t = "avoid" if (w["duration_p50_days"] > 100 or w["n_laterals"] >= 4) else "none"
            if not self.tags[w["well_id"]] and self.rng.random() < 0.10:
                i = order.index(t)
                i = i + 1 if i == 0 else (i - 1 if i == 2 else i + int(self.rng.choice([-1, 1])))
                t = order[i]
            w["winter_tier"] = w.get("tier_override") or t

    def capex(self):
        for w in self.wells.values():
            p50 = w["duration_p50_days"]
            if w["installation_type"] == "platform":
                spread = self.u(0.5, 0.7)
            elif w["installation_type"] == "subsea_template":
                spread = self.u(0.7, 1.0)
            else:
                spread = self.u(0.5, 0.85) if w["water_depth_m"] <= 140 else self.u(0.7, 1.0)
            spread *= 1.15 if w["is_hpht"] else 1.0
            rig = p50 * spread * 1e6
            mat = rig * 0.8 * self.u(0.85, 1.15)
            oth = rig * 0.2 * self.u(0.85, 1.15)
            tie = w["installation_type"] == "subsea_template" and self.rng.random() < 0.3
            if tie:
                oth += self.u(20e6, 40e6)
            w.update(capex_rig_usd=round(rig, -3), capex_materials_usd=round(mat, -3),
                     capex_other_usd=round(oth, -3), has_new_tie_in=bool(tie))
            w["capex_usd"] = w["capex_rig_usd"] + w["capex_materials_usd"] + w["capex_other_usd"]
            w["rig_day_cost_usd"] = round(w["capex_rig_usd"] / p50, -2)
            w["opex_fixed_usd_per_year"] = round(self.u(0.3e6, 1.0e6), -3) if w["category"] == "development" else 0.0
            w["opex_variable_usd_per_boe"] = round(self.u(6, 14), 2) if w["category"] == "development" else 0.0

    def arps(self, qi, plateau, b, de):
        t = np.arange(PROFILE_MONTHS, dtype=float)
        di = ((1 - de) ** (-b) - 1) / b
        tt = np.clip(t - plateau, 0, None) / 12.0
        q = np.where(t < plateau, qi, qi / (1 + b * di * tt) ** (1 / b))
        return q * 30.4  # boe per month

    def profiles(self):
        self.prof = {}
        typical = {}
        for w in self.wells.values():
            if w["category"] != "development":
                continue
            asset = w["asset"]
            boe = self.arps(self.u(*ASSET_QI[asset]), int(self.rng.integers(3, 19)), self.u(0.3, 0.9), self.u(0.15, 0.45))
            eur = boe.sum()
            target = float(np.clip(eur, 1e6, 15e6))
            boe *= target / eur
            if w["purpose"] == "producer":
                if w["eur_override"]:
                    boe *= w["eur_override"] / boe.sum()
                else:
                    boe *= w["value_boost"]
                    if boe.sum() > 15e6:
                        boe *= 15e6 / boe.sum()
                inc = False
                typical.setdefault(w["field_id"], boe)
            else:
                share = self.u(0.2, 0.5) if w["purpose"] != "observation" else self.u(0.12, 0.25)
                lag = int(self.rng.integers(6, 13)) if w["purpose"] != "observation" else 6
                boe = np.concatenate([np.zeros(lag), boe[:-lag]]) * share * w["value_boost"]
                inc = True
            oil = boe * w["oil_share"]
            gas = boe * (1 - w["oil_share"]) * MMBTU_PER_BOE
            self.prof[w["well_id"]] = (oil, gas, inc)
            w["is_incremental"] = inc
            w["eur_mmboe"] = round(boe.sum() / 1e6, 3)

    # ------------------------------------------------------------------ campaigns & precedence
    def build_campaigns(self):
        prefer = ["Kameleon", "Valhall Flank North", "Fulla T-A", "Frøy T-A", "Hugin A", "Munin"]
        busy = {}
        for w in self.wells.values():
            if w["installation_id"]:
                busy.setdefault(w["installation_id"], []).append(w)
        def ok(w):
            return not self.tags[w["well_id"]] and w["purpose"] in ("producer", "water_injector", "gas_injector")
        insts = sorted(self.insts, key=lambda i: (prefer.index(i["name"]) if i["name"] in prefer else 99,
                                                   -len(busy.get(i["installation_id"], []))))
        n = 0
        for inst in insts:
            if n >= self.p["n_campaigns"]:
                break
            ws = [w for w in busy.get(inst["installation_id"], []) if ok(w)]
            min_k = 2 if self.size == "small" else 4
            if len(ws) < min_k:
                continue
            k = min(int(self.rng.integers(4, 9)), len(ws))
            ws = ws[:k]
            n += 1
            cid = f"BC-{n:02d}"
            start = max(w["earliest_start"] for w in ws)
            for w in ws:
                w.update(batch_campaign_id=cid, earliest_start=start, must_complete_by=None)
                self.tag(w["well_id"], "conductor_campaign")
                top = self.u(5, 12)
                t10, t90 = round(top * 0.8, 1), round(top * 1.3, 1)
                top = round(top, 1)
                self.phases[w["well_id"]] = [
                    dict(phase="top_hole", duration_p10_days=t10, duration_p50_days=top, duration_p90_days=t90,
                         weather_sensitivity=round(self.u(0.1, 0.2), 2), capex_share=0.15, min_lag_days=0),
                    dict(phase="main", duration_p10_days=round(w["duration_p10_days"] - t10, 1),
                         duration_p50_days=round(w["duration_p50_days"] - top, 1),
                         duration_p90_days=round(w["duration_p90_days"] - t90, 1),
                         weather_sensitivity=round(w["weather_sensitivity"], 2), capex_share=0.85, min_lag_days=0),
                ]
            self.campaigns.append(dict(batch_campaign_id=cid, installation_id=inst["installation_id"],
                                       well_ids=[w["well_id"] for w in ws]))
            if n == 1:
                for w in ws:
                    w["value_boost"] = 1.3

    def build_precedence(self):
        chain = {w for w, ts in self.tags.items() if "precedence_chain" in ts}
        for w in self.wells.values():
            if w["purpose"] not in ("water_injector", "gas_injector") or w["batch_campaign_id"] or w["well_id"] in chain:
                continue
            if self.rng.random() >= 0.5:
                continue
            prods = [p for p in self.wells.values() if p["field_id"] == w["field_id"] and p["purpose"] == "producer"
                     and not p["batch_campaign_id"] and p["well_id"] not in chain]
            if not prods:
                continue
            k = min(len(prods), int(self.rng.integers(1, 4)))
            for i in self.rng.choice(len(prods), k, replace=False):
                self.preds.append((prods[i]["well_id"], w["well_id"], int(self.rng.integers(0, 91))))

    # ------------------------------------------------------------------ economics
    def economics(self):
        years = list(range(PLAN_START.year, self.horizon_end.year + TAIL_YEARS + 8))
        oil, gas = [], []
        for y in years:
            o = 75 if y == 2027 else 70 if y == 2028 else 68 * (1 + self.rng.normal(0, 0.03))
            g = 11 if y == 2027 else 9 if y == 2028 else 8 * (1 + self.rng.normal(0, 0.03))
            oil.append(round(o, 2)); gas.append(round(g, 2))
        self.price = pd.DataFrame(dict(year=years, oil_usd_bbl=oil, gas_usd_mmbtu=gas, fx_nok_usd=FX))
        nm = len(years) * 12
        yidx = np.arange(nm) // 12
        self.oil_p = np.array(oil)[yidx]
        self.gas_p = np.array(gas)[yidx]
        self.df = (1 + DISCOUNT_RATE) ** (-np.arange(nm) / 12.0)
        self.L = 12 * self.p["horizon_years"] + 36  # completion months covered by value tables
        self.value_table = {}
        y_oil, y_gas = self.oil_p * self.df, self.gas_p * self.df
        for w in self.wells.values():
            dm = int(round(w["duration_p50_days"] / 30.44))
            cap_idx = np.clip(np.arange(self.L) - dm, 0, None)
            capex = w["capex_usd"] * self.df[cap_idx]
            if w["category"] == "development":
                o, g, _ = self.prof[w["well_id"]]
                boe = o + g / MMBTU_PER_BOE
                opex = w["opex_fixed_usd_per_year"] / 12 + w["opex_variable_usd_per_boe"] * boe
                seg = slice(1, 1 + self.L + PROFILE_MONTHS - 1)
                rev = np.correlate(y_oil[seg], o, "valid") + np.correlate(y_gas[seg], g, "valid")
                opx = np.correlate(self.df[seg], opex, "valid")
                v = rev - opx - capex
            else:
                lag = w["discovery_value_lag_months"]
                v = w["probability_of_success"] * w["discovery_value_usd"] * self.df[np.arange(self.L) + lag] - capex
            self.value_table[w["well_id"]] = v
            c0 = min(self.L - 1, dm)
            w["value_at_plan_start_usd"] = round(float(v[c0]), -3)

    def weather(self):
        rows = []
        for m in range(1, 13):
            vals = []
            for r in ["north_sea_south", "north_sea_north", "norwegian_sea", "barents_sea"]:
                vals.append(WOW_BASE[r][MONTH_CLASS[m]] * (1 + self.rng.uniform(-0.1, 0.1)))
            vals = sorted(vals)  # keep south < north < norwegian < barents
            for r, v in zip(["north_sea_south", "north_sea_north", "norwegian_sea", "barents_sea"], vals):
                rows.append(dict(region=r, month=m, wow_factor=round(v, 4)))
        self.wow = pd.DataFrame(rows)
        wd = {r: [float(self.wow[(self.wow.region == r) & (self.wow.month == m)].wow_factor.iloc[0])
                  for m in range(1, 13)] for r in REGION_BOX}
        self.cal = Calendar(PLAN_START, self.H + 4000, wd)

    def exploration_targets(self):
        years = list(range(self.freeze_end.year, self.horizon_end.year))
        n_ok = sum(1 for w in self.wells.values() if w["category"] != "development"
                   and "no_compatible_rig" not in self.tags[w["well_id"]])
        avg = 0.8 * n_ok / self.p["horizon_years"]
        rows = []
        bind = years[min(2, len(years) - 1)]
        for y in years:
            mn = max(1, round(self.u(0.55, 0.8) * avg))
            mx = max(mn + 1, round(self.u(1.2, 1.5) * avg))
            tag = ""
            if y == bind:
                mn = max(mn + 1, math.ceil(1.4 * avg)); mx = mn + 2; tag = "binding_target"
            rows.append(dict(year=y, min_wells=mn, max_wells=mx, test_tag=tag))
        while sum(r["min_wells"] for r in rows) > 0.85 * n_ok:
            r = max(rows, key=lambda r: r["min_wells"] if not r["test_tag"] else -1)
            r["min_wells"] -= 1
        self.targets = pd.DataFrame(rows)

    # ------------------------------------------------------------------ reference schedule (greedy)
    def loc(self, wid):
        w = self.wells[wid]
        return dict(well_id=wid, installation_id=w["installation_id"] or None, lat=w["lat"], lon=w["lon"],
                    region=w["region"])

    def month_idx(self, day):
        d = PLAN_START + timedelta(days=int(day))
        return (d.year - PLAN_START.year) * 12 + d.month - 1

    def seg_spec(self, wid, phase):
        w = self.wells[wid]
        if phase == "full":
            return w["duration_p50_days"], w["weather_sensitivity"]
        ph = next(p for p in self.phases[wid] if p["phase"] == phase)
        return ph["duration_p50_days"], ph["weather_sensitivity"]

    def place(self, job, rig, st):
        cal = self.cal
        free, loc = st["free"], st["loc"]
        first = self.loc(job["segments"][0][0])
        frm = loc if loc is not None else dict(well_id=None, installation_id=None, lat=rig["port"][0],
                                              lon=rig["port"][1], region="north_sea_north")
        depart = st["last_end"]  # sailing is measured from the end of the previous well
        sail0 = sail_days(cal, rig, frm, first, depart, SAME_INST_MOVE)
        s = max(free, depart + sail0, job["earliest"], rig["cs"])
        for p, lag in job["preds"]:
            s = max(s, self.sched_end[p] + lag)
        for _ in range(80):
            segs, t, prev = [], s, None
            for wid, ph in job["segments"]:
                mv = sail0
                if prev is not None:
                    mv = sail_days(cal, rig, self.loc(prev), self.loc(wid), t, SAME_INST_MOVE)
                    t += mv
                base, sens = self.seg_spec(wid, ph)
                eff = effective_duration(cal, base, sens, self.wells[wid]["winter_tier"], self.wells[wid]["region"], t)
                segs.append([wid, ph, t, t + eff, mv, base])
                t += eff
                prev = wid
            end = t
            shift = None
            for us, ue in rig["unavail"]:
                if any(a < ue and us < b for _, _, a, b, _, _ in segs):
                    shift = ue if shift is None else max(shift, ue)
            if shift is None and job["inst"]:
                for a, b in self.inst_busy[job["inst"]]:
                    if a < end and s < b:
                        shift = b if shift is None else max(shift, b)
            if shift is None:
                break
            s = max(shift, s + 1)
        else:
            return None
        if end > rig["ce"] or s >= self.H:
            return None
        for wid in job["wells"]:
            w = self.wells[wid]
            ws = [g for g in segs if g[0] == wid]
            st_w, en_w = ws[0][2], ws[-1][3]
            if w["latest_start"] and st_w > self.day(w["latest_start"]):
                return None
            if w["must_complete_by"] and en_w > self.day(w["must_complete_by"]):
                return None
            if w["strategic_window_end"] and en_w > self.day(w["strategic_window_end"]):
                return None
        return segs

    def job_value(self, job, segs):
        v = 0.0
        for wid in job["wells"]:
            end = max(g[3] for g in segs if g[0] == wid)
            c = min(self.L - 1, max(0, self.month_idx(end)))
            v += float(self.value_table[wid][c])
        pen = 0.0
        for wid, ph, a, b, mv, base in segs:
            w = self.wells[wid]
            pen += WINTER_PENALTY_SHARE[w["winter_tier"]] * w["rig_day_cost_usd"] * max(0.0, (b - a) - base)
        return v, pen

    def build_jobs(self):
        jobs = []
        in_campaign = set()
        for c in self.campaigns:
            ws = c["well_ids"]
            in_campaign.update(ws)
            segs = [(w, "top_hole") for w in ws] + [(ws[-1], "main")] + [(w, "main") for w in ws[:-1]]
            jobs.append(dict(id=c["batch_campaign_id"], wells=ws, segments=segs))
        for wid, w in self.wells.items():
            if wid not in in_campaign:
                jobs.append(dict(id=wid, wells=[wid], segments=[(wid, "full")]))
        pred_map = defaultdict(list)
        for succ, pred, lag in self.preds:
            pred_map[succ].append((pred, lag))
        for j in jobs:
            ws = [self.wells[w] for w in j["wells"]]
            j["earliest"] = max(self.day(w["earliest_start"]) for w in ws)
            j["inst"] = ws[0]["installation_id"] or None
            j["preds"] = [p for w in j["wells"] for p in pred_map[w]]
            j["rigs"] = set.intersection(*[set(self.compat_rigs(w)) for w in ws])
            j["mandatory"] = any(w["is_mandatory"] for w in ws)
            j["deadline"] = min([self.day(w["must_complete_by"]) for w in ws if w["must_complete_by"]] or [10 ** 9])
            j["v0"] = sum(w["value_at_plan_start_usd"] for w in ws)
            j["nominal"] = sum(w["duration_p50_days"] for w in ws) + 2 * len(ws)
        # injectors etc. that unlock producers get credit for part of the value they enable
        succs = defaultdict(list)
        for succ, pred, lag in self.preds:
            succs[pred].append(succ)
        for j in jobs:
            j["enable"] = 0.5 * sum(max(0.0, self.wells[s]["value_at_plan_start_usd"])
                                    for w in j["wells"] for s in succs[w])
            j["score0"] = (j["v0"] + j["enable"]) / j["nominal"]
        return jobs

    def schedule(self):
        for r in self.rigs:
            r["cs"], r["ce"] = self.day(r["contract_start"]), self.day(r["contract_end"])
            r["unavail"] = [(self.day(u["start"]), self.day(u["end"])) for u in self.unavail if u["rig_id"] == r["rig_id"]]
            r["port"] = PORTS[r["rig_type"]]
        jobs = self.build_jobs()
        self.sched_end, self.inst_busy = {}, defaultdict(list)
        self.assign = []
        state = {r["rig_id"]: dict(free=max(0, r["cs"]), last_end=max(0, r["cs"]), loc=None) for r in self.rigs}
        done = set()

        def commit(job, rig, segs):
            for wid, ph, a, b, mv, base in segs:
                self.assign.append(dict(well_id=wid, phase=ph, rig_id=rig["rig_id"], start=a, end=b,
                                        base=base, eff=b - a, sail_in=mv))
            for wid in job["wells"]:
                self.sched_end[wid] = max(g[3] for g in segs if g[0] == wid)
            if job["inst"]:
                self.inst_busy[job["inst"]].append((segs[0][2], segs[-1][3]))
            state[rig["rig_id"]] = dict(free=segs[-1][3], last_end=segs[-1][3], loc=self.loc(segs[-1][0]))
            done.add(job["id"])

        # in-progress wells at plan start
        used_inst = set()
        for r in self.rigs:
            if r["cs"] > 0:
                continue
            cands = [j for j in jobs if r["rig_id"] in j["rigs"] and len(j["wells"]) == 1 and not j["preds"]
                     and j["earliest"] <= 0 and j["v0"] > 0 and not self.tags[j["id"]] and j["id"] not in done
                     and j["inst"] not in used_inst and j["deadline"] > 10 ** 8]
            if not cands:
                continue
            j = cands[int(self.rng.integers(0, len(cands)))]
            w = self.wells[j["id"]]
            s = -int(self.rng.integers(5, max(6, int(w["duration_p50_days"] * 0.6))))
            eff = effective_duration(self.cal, w["duration_p50_days"], w["weather_sensitivity"], w["winter_tier"], w["region"], s)
            commit(j, r, [[j["id"], "full", s, s + eff, 0, w["duration_p50_days"]]])
            used_inst.add(j["inst"])
            w["status"] = "in_progress"
            r["initial_well_id"] = j["id"]
        for r in self.rigs:
            if state[r["rig_id"]]["loc"] is None:
                r["initial_well_id"] = ""

        heap = [(state[r["rig_id"]]["free"], i) for i, r in enumerate(self.rigs)]
        heapq.heapify(heap)
        while heap:
            free, i = heapq.heappop(heap)
            rig = self.rigs[i]
            st = state[rig["rig_id"]]
            if st["free"] >= min(self.H, rig["ce"]):
                continue
            cands = [j for j in jobs if j["id"] not in done and rig["rig_id"] in j["rigs"]
                     and all(p in self.sched_end for p, _ in j["preds"])
                     and j["earliest"] <= st["free"] + 150 and (j["v0"] + j["enable"] > 0 or j["mandatory"])]
            cands.sort(key=lambda j: (j["mandatory"], j["deadline"] < st["free"] + 500, j["score0"]), reverse=True)
            best, best_score = None, -float("inf")
            for j in cands[:20]:
                segs = self.place(j, rig, st)
                if segs is None:
                    continue
                v, pen = self.job_value(j, segs)
                span = segs[-1][3] - st["free"]
                score = (v + j["enable"] - pen) / max(1, span)
                if j["mandatory"]:
                    score += 1e12
                if j["deadline"] < 10 ** 8:
                    score += 1e10
                if score > best_score:
                    best, best_score = (j, segs), score
            if best is None:
                st["free"] += 20
                heapq.heappush(heap, (st["free"], i))
                continue
            commit(best[0], rig, best[1])
            heapq.heappush(heap, (state[rig["rig_id"]]["free"], i))

        for a in self.assign:
            a["is_frozen"] = a["start"] < self.freeze_day
        frozen_wells = defaultdict(list)
        for a in self.assign:
            frozen_wells[a["well_id"]].append(a["is_frozen"])
        for wid, fl in frozen_wells.items():
            w = self.wells[wid]
            if w["status"] != "in_progress" and all(fl):
                w["status"] = "frozen"
        over = [a for a in self.assign if a["is_frozen"] and a["end"] > self.freeze_day]
        if over:
            self.tag(over[0]["well_id"], "frozen_overrun")
        else:
            self.notes.append("No frozen assignment crosses the freeze boundary in this draw.")

    # ------------------------------------------------------------------ output
    def write(self, out: Path):
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        cfg = dict(size=self.size, seed=self.seed, unit_set="USD_bbl_MMBtu", plan_start=PLAN_START.isoformat(),
                   frozen_months=FROZEN_MONTHS, freeze_end=self.freeze_end.isoformat(),
                   horizon_years=self.p["horizon_years"], horizon_end=self.horizon_end.isoformat(),
                   duration_percentile=DURATION_PERCENTILE, discount_rate=DISCOUNT_RATE,
                   production_tail_years=TAIL_YEARS, same_installation_move_days=SAME_INST_MOVE,
                   rig_exclusion_level="installation", winter_months="11,12,1,2",
                   tier_factor_none=TIER_FACTOR["none"], tier_factor_avoid=TIER_FACTOR["avoid"],
                   tier_factor_strongly_avoid=TIER_FACTOR["strongly_avoid"],
                   winter_penalty_share_avoid=WINTER_PENALTY_SHARE["avoid"],
                   winter_penalty_share_strongly_avoid=WINTER_PENALTY_SHARE["strongly_avoid"],
                   fx_nok_per_usd=FX, optimization_freedom="free", n_variants=1)
        (out / "config.yaml").write_text("".join(f"{k}: {v}\n" for k, v in cfg.items()), encoding="utf-8")
        pd.DataFrame(self.assets).to_csv(out / "assets.csv", index=False)
        pd.DataFrame(self.fields).drop(columns=["oil_share"]).to_csv(out / "fields.csv", index=False)
        pd.DataFrame(self.insts).drop(columns=["asset"]).to_csv(out / "installations.csv", index=False)
        rig_rows = []
        for r in self.rigs:
            init = self.wells.get(r.get("initial_well_id") or "", None)
            rig_rows.append(dict(
                rig_id=r["rig_id"], name=r["name"], rig_type=r["rig_type"],
                min_water_depth_m=r["min_water_depth_m"], max_water_depth_m=r["max_water_depth_m"],
                max_drilling_depth_m=r["max_drilling_depth_m"], capabilities=";".join(sorted(r["capabilities"])),
                transit_speed_knots=r["transit_speed_knots"], mob_demob_days=r["mob_demob_days"],
                move_sensitivity=r["move_sensitivity"], contract_start=fmt(r["contract_start"]),
                contract_end=fmt(r["contract_end"]), extension_option=r["extension_option"],
                initial_well_id=r.get("initial_well_id", ""),
                initial_lat=init["lat"] if init else r["port"][0], initial_lon=init["lon"] if init else r["port"][1],
                day_rate_usd=r["day_rate_usd"], performance_factor=r["performance_factor"]))
        pd.DataFrame(rig_rows).to_csv(out / "rigs.csv", index=False)
        pd.DataFrame([dict(rig_id=u["rig_id"], start=fmt(u["start"]), end=fmt(u["end"]), reason=u["reason"])
                      for u in self.unavail]).to_csv(out / "rig_unavailability.csv", index=False)
        cols = ["well_id", "name", "asset", "field_id", "installation_id", "installation_type", "region", "block",
                "lat", "lon", "water_depth_m", "measured_depth_m", "tvd_m", "category", "purpose", "fluid",
                "is_hpht", "is_multilateral", "n_laterals", "required_capabilities", "allowed_rig_types",
                "allowed_rigs", "excluded_rigs", "duration_p10_days", "duration_p50_days", "duration_p90_days",
                "discovery_duration_p50_days", "weather_sensitivity", "winter_tier", "batch_campaign_id", "is_phased",
                "earliest_start", "latest_start", "must_complete_by", "is_mandatory", "capex_usd", "capex_rig_usd",
                "capex_materials_usd", "capex_other_usd", "has_new_tie_in", "rig_day_cost_usd",
                "opex_fixed_usd_per_year", "opex_variable_usd_per_boe", "is_incremental", "eur_mmboe",
                "probability_of_success", "discovery_value_usd", "discovery_value_lag_months", "is_strategic",
                "strategic_window_start", "strategic_window_end", "prospect_name", "license",
                "value_at_plan_start_usd", "status", "test_tag"]
        rows = []
        for w in self.wells.values():
            r = dict(w)
            for k in ("required_capabilities", "allowed_rig_types", "allowed_rigs", "excluded_rigs"):
                r[k] = ";".join(sorted(w[k]))
            for k in ("earliest_start", "latest_start", "must_complete_by", "strategic_window_start", "strategic_window_end"):
                r[k] = fmt(w[k])
            r["weather_sensitivity"] = round(w["weather_sensitivity"], 2)
            r["is_phased"] = w["well_id"] in self.phases
            r["is_incremental"] = w.get("is_incremental", False)
            r["eur_mmboe"] = w.get("eur_mmboe", 0.0)
            r["test_tag"] = ";".join(self.tags[w["well_id"]])
            rows.append({c: r.get(c, "") for c in cols})
        pd.DataFrame(rows, columns=cols).to_csv(out / "wells.csv", index=False)
        pd.DataFrame([dict(well_id=s, predecessor_id=p, min_lag_days=l) for s, p, l in self.preds],
                     columns=["well_id", "predecessor_id", "min_lag_days"]).to_csv(out / "well_predecessors.csv", index=False)
        pd.DataFrame([dict(well_id=w, **ph) for w, phs in self.phases.items() for ph in phs],
                     columns=["well_id", "phase", "duration_p10_days", "duration_p50_days", "duration_p90_days",
                              "weather_sensitivity", "capex_share", "min_lag_days"]).to_csv(out / "well_phases.csv", index=False)
        pd.DataFrame([dict(c, well_ids=";".join(c["well_ids"])) for c in self.campaigns],
                     columns=["batch_campaign_id", "installation_id", "well_ids"]).to_csv(out / "batch_campaigns.csv", index=False)
        prof_rows = []
        for wid, (o, g, inc) in self.prof.items():
            m = np.arange(1, PROFILE_MONTHS + 1)
            keep = (o > 0) | (g > 0)
            prof_rows.append(pd.DataFrame(dict(well_id=wid, month_index=m[keep], oil_bbl=np.round(o[keep], 1),
                                               gas_mmbtu=np.round(g[keep], 1), is_incremental=inc)))
        pd.concat(prof_rows).to_csv(out / "production_profiles.csv", index=False)
        self.targets.to_csv(out / "exploration_targets.csv", index=False)
        self.wow.to_csv(out / "weather.csv", index=False)
        self.price.to_csv(out / "price_deck.csv", index=False)
        for name, f in (("price_deck_low.csv", 0.75), ("price_deck_high.csv", 1.25)):
            p = self.price.copy()
            p["oil_usd_bbl"] = (p.oil_usd_bbl * f).round(2)
            p["gas_usd_mmbtu"] = (p.gas_usd_mmbtu * f).round(2)
            p.to_csv(out / name, index=False)
        sched = pd.DataFrame([dict(well_id=a["well_id"], phase=a["phase"], rig_id=a["rig_id"],
                                   start_date=fmt(self.cal.to_date(a["start"])), end_date=fmt(self.cal.to_date(a["end"])),
                                   base_duration_days=a["base"], effective_duration_days=a["eff"],
                                   sail_days_in=a["sail_in"], is_frozen=a["is_frozen"]) for a in self.assign])
        sched = sched.sort_values(["rig_id", "start_date"]).reset_index(drop=True)
        sched.to_csv(out / "reference_schedule.csv", index=False)
        sched[sched.is_frozen].to_csv(out / "frozen_schedule.csv", index=False)
        comp = pd.DataFrame([{**{"well_id": w["well_id"]}, **{r["rig_id"]: compatible(r, w) for r in self.rigs}}
                             for w in self.wells.values()])
        comp.to_csv(out / "compatibility_matrix.csv", index=False)
        self.write_readme(out, pd.DataFrame(rows), sched, comp)

    def write_readme(self, out, wells, sched, comp):
        dev = wells[wells.category == "development"]
        ex = wells[wells.category != "development"]
        ncomp = comp.drop(columns="well_id").sum(axis=1)
        tags = defaultdict(int)
        for t in wells.test_tag:
            for x in filter(None, str(t).split(";")):
                tags[x] += 1
        horizon_days = self.H
        util = []
        for r in self.rigs:
            s = sched[sched.rig_id == r["rig_id"]]
            drill = int(s.effective_duration_days.sum())
            sail = int(s.sail_days_in.sum())
            fz = s[s.is_frozen]
            fz_days = int(np.clip(pd.to_datetime(fz.end_date).map(lambda d: (d.date() - PLAN_START).days).clip(upper=self.freeze_day)
                                  - pd.to_datetime(fz.start_date).map(lambda d: (d.date() - PLAN_START).days).clip(lower=0), 0, None).sum()) if len(fz) else 0
            util.append(f"| {r['rig_id']} {r['name']} | {r['rig_type']} | {';'.join(sorted(r['capabilities']))} | "
                        f"{len(s)} | {drill} | {sail} | {100 * fz_days / self.freeze_day:.0f}% |")
        unsched_mand = [w for w in self.wells.values() if w["is_mandatory"] and w["well_id"] not in self.sched_end]
        lines = [
            f"# Synthetic rig-schedule dataset — `{self.size}` (seed {self.seed})", "",
            "Generated by `generate_data.py` for spec v0.4. All values are synthetic; field names are real but "
            "coordinates, blocks and water depths are approximate. Rig names are fictional.", "",
            f"Plan start {PLAN_START}, frozen until {self.freeze_end}, horizon end {self.horizon_end}. "
            "Dates in schedules: rig occupied on [start_date, end_date).", "",
            "## Wells", "",
            f"- Development wells: {len(dev)}; exploration: {int((ex.category == 'exploration').sum())}; appraisal: {int((ex.category == 'appraisal').sum())}",
            f"- HPHT wells: {int(wells.is_hpht.sum())}; multilateral: {int(wells.is_multilateral.sum())}; phased (batch): {int(wells.is_phased.sum())}",
            f"- Strategic exploration wells: {int(wells.is_strategic.sum())}",
            f"- Total EUR (incl. incremental): {dev.eur_mmboe.sum():.0f} MMboe",
            f"- Total risked exploration value at plan start: USD {ex.value_at_plan_start_usd.sum() / 1e6:,.0f} M", "",
            "| Asset | Dev wells | Share |", "|---|---|---|",
            *[f"| {a} | {n} | {100 * n / len(dev):.1f}% |" for a, n in dev.asset.value_counts().items()], "",
            "| Category | Count |", "|---|---|",
            *[f"| {k} | {v} |" for k, v in wells.purpose.value_counts().items()], "",
            "| Winter tier | Count | Share |", "|---|---|---|",
            *[f"| {k} | {v} | {100 * v / len(wells):.0f}% |" for k, v in wells.winter_tier.value_counts().items()], "",
            "**Compatible rigs per well (histogram):** " + ", ".join(f"{int(k)} rigs: {v}" for k, v in sorted(ncomp.value_counts().items())), "",
            "## Rigs and reference schedule", "",
            "| Rig | Type | Capabilities | Assignments | Drilling days | Sailing days | Frozen-period utilization |",
            "|---|---|---|---|---|---|---|", *util, "",
            f"Reference schedule: {sched.well_id.nunique()} wells scheduled, {len(sched[sched.is_frozen])} frozen assignments. "
            "It is a simple greedy plan meant as a 'last approved' baseline, not an optimum, and does not try to meet exploration targets.",
            "" if not unsched_mand else f"Mandatory wells not placed by the greedy baseline: {', '.join(w['well_id'] for w in unsched_mand)}.", "",
            "## Edge cases (`test_tag`)", "", "| Tag | Wells |", "|---|---|",
            *[f"| {k} | {v} |" for k, v in sorted(tags.items())],
            f"| binding_target | year {', '.join(str(y) for y in self.targets[self.targets.test_tag == 'binding_target'].year)} in exploration_targets.csv |", "",
            "## Conventions", "",
            "- Effective duration = ceil(P50 × (1 + weather_sensitivity × tier_factor × mean wow_factor over [start, start+ceil(P50)))).",
            "- Sailing = ceil((nm / (knots×24) + mob_demob_days) × (1 + move_sensitivity × wow_factor(destination, departure day))); "
            "same installation = 1 day; next phase of the same well = 0.",
            "- Rig unavailability = rig out of service in place.",
            "- Value tables: production month 1 = first month after completion; monthly discounting at 8 %/yr from plan start.",
            "- Observation wells carry a small incremental profile (reservoir-management value) so they are not automatically negative.",
            *[f"- Note: {n}" for n in self.notes],
        ]
        (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run(self, out: Path):
        self.build_locations()
        self.build_rigs()
        self.build_dev_wells()
        self.build_expl_wells()
        self.edge_geometry()
        self.fix_compatibility()
        self.durations()
        self.weather()
        self.edge_attributes()
        self.tiers()
        self.capex()
        self.profiles()
        self.build_campaigns()
        self.build_precedence()
        for w in self.wells.values():  # schedule with exactly the values written to CSV
            w["weather_sensitivity"] = round(w["weather_sensitivity"], 2)
        self.economics()
        self.exploration_targets()
        self.schedule()
        self.write(out)


def convert_nok(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    w = pd.read_csv(dst / "wells.csv")
    for c in ["capex_usd", "capex_rig_usd", "capex_materials_usd", "capex_other_usd", "rig_day_cost_usd",
              "discovery_value_usd", "value_at_plan_start_usd"]:
        w[c.replace("_usd", "_nok")] = w[c] * FX
        w = w.drop(columns=c)
    w["opex_fixed_nok_per_year"] = w.pop("opex_fixed_usd_per_year") * FX
    w["opex_variable_nok_per_sm3oe"] = w.pop("opex_variable_usd_per_boe") * FX * BOE_PER_SM3
    w["eur_msm3oe"] = w.pop("eur_mmboe") / BOE_PER_SM3
    w.to_csv(dst / "wells.csv", index=False)
    r = pd.read_csv(dst / "rigs.csv")
    r["day_rate_nok"] = r.pop("day_rate_usd") * FX
    r.to_csv(dst / "rigs.csv", index=False)
    p = pd.read_csv(dst / "production_profiles.csv")
    p["oil_sm3"] = p.pop("oil_bbl") / BOE_PER_SM3
    p["gas_sm3"] = p.pop("gas_mmbtu") / MMBTU_PER_SM3_GAS
    p = p[["well_id", "month_index", "oil_sm3", "gas_sm3", "is_incremental"]]
    p.to_csv(dst / "production_profiles.csv", index=False)
    for f in ("price_deck.csv", "price_deck_low.csv", "price_deck_high.csv"):
        d = pd.read_csv(dst / f)
        d["oil_nok_sm3"] = d.pop("oil_usd_bbl") * d.fx_nok_usd * BOE_PER_SM3
        d["gas_nok_sm3"] = d.pop("gas_usd_mmbtu") * d.fx_nok_usd * MMBTU_PER_SM3_GAS
        d.to_csv(dst / f, index=False)
    cfg = (dst / "config.yaml").read_text(encoding="utf-8").replace("unit_set: USD_bbl_MMBtu", "unit_set: NOK_Sm3")
    (dst / "config.yaml").write_text(cfg, encoding="utf-8")
    readme = (dst / "README.md").read_text(encoding="utf-8")
    (dst / "README.md").write_text(
        f"> Unit set NOK_Sm3: converted from `{src.name}` at {FX} NOK/USD, {BOE_PER_SM3} boe/Sm³, "
        f"{MMBTU_PER_SM3_GAS} MMBtu/Sm³ gas. Statistics below are in the original units.\n\n" + readme, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", choices=list(SIZES) + ["all"], default="default")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--unit-set", choices=["USD_bbl_MMBtu", "NOK_Sm3", "both"], default="USD_bbl_MMBtu")
    ap.add_argument("--out", default=str(Path(__file__).parent / "data"))
    a = ap.parse_args()
    sizes = list(SIZES) if a.size == "all" else [a.size]
    for s in sizes:
        out = Path(a.out) / s
        Generator(s, a.seed).run(out)
        print(f"wrote {out}")
        if a.unit_set in ("NOK_Sm3", "both"):
            convert_nok(out, Path(a.out) / f"{s}_NOK_Sm3")
            print(f"wrote {Path(a.out) / (s + '_NOK_Sm3')}")
            if a.unit_set == "NOK_Sm3":
                shutil.rmtree(out)


if __name__ == "__main__":
    main()
