"""Shared conventions for the synthetic rig-schedule data (spec v0.4).

Both the generator and the validator import this module, so the frozen and
reference schedules are checked with exactly the same rules they were built with.

Conventions
-----------
* Time is in whole days, as an integer day index relative to ``plan_start``.
  An assignment occupies the half-open interval [start, end).
* Effective duration (spec §6.3, §7):
      eff = ceil(base * (1 + sensitivity * tier_factor[tier] * mean_wow))
  where mean_wow is the mean wow_factor of the well's region over the calendar
  days [start, start + ceil(base)).  Using the base window avoids a circular
  definition.
* Sailing (spec §7, §5.2):
      same installation  -> same_installation_move_days
      same well (phase to phase) -> 0
      otherwise          -> nm / (knots * 24) + mob_demob_days
  multiplied by (1 + move_sensitivity * wow(destination region, departure day)),
  then rounded up.  Departure day = end of the previous assignment.
* Rig unavailability = rig out of service in place; no assignment may overlap it.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np

TIER_FACTOR = {"none": 1.0, "avoid": 1.5, "strongly_avoid": 3.5}
WINTER_PENALTY_SHARE = {"none": 0.0, "avoid": 0.10, "strongly_avoid": 0.50}
REGIONS = ["north_sea_south", "north_sea_north", "norwegian_sea", "barents_sea"]
REGION_BOX = {  # (lat_min, lat_max, lon_min, lon_max)
    "north_sea_south": (56.0, 57.5, 1.5, 4.5),
    "north_sea_north": (57.5, 62.0, 1.0, 4.5),
    "norwegian_sea": (63.0, 68.0, 4.0, 10.0),
    "barents_sea": (70.0, 74.0, 15.0, 35.0),
}
DAY_OFFSET = 400  # calendar arrays start this many days before plan_start
BOE_PER_SM3 = 6.2898
MMBTU_PER_SM3_GAS = 0.0353
MMBTU_PER_BOE = 5.8


def great_circle_nm(lat1, lon1, lat2, lon2) -> float:
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(min(1.0, a)))


class Calendar:
    """Maps day index -> month of year and weather factor per region."""

    def __init__(self, plan_start: date, n_days: int, wow: dict[str, list[float]]):
        self.plan_start = plan_start
        self.n = n_days + DAY_OFFSET
        months = np.array(
            [(plan_start + timedelta(days=d - DAY_OFFSET)).month for d in range(self.n)]
        )
        self.month_of_year = months
        self.wow_day = {r: np.array(wow[r])[months - 1] for r in wow}
        self.cum = {r: np.concatenate([[0.0], np.cumsum(v)]) for r, v in self.wow_day.items()}

    def to_date(self, day: int) -> date:
        return self.plan_start + timedelta(days=int(day))

    def to_day(self, d: date) -> int:
        return (d - self.plan_start).days

    def mean_wow(self, region: str, start: int, length: int) -> float:
        a = start + DAY_OFFSET
        b = a + max(1, length)
        c = self.cum[region]
        return float((c[b] - c[a]) / (b - a))

    def wow(self, region: str, day: int) -> float:
        return float(self.wow_day[region][day + DAY_OFFSET])


def effective_duration(cal: Calendar, base: float, sensitivity: float, tier: str,
                       region: str, start: int) -> int:
    length = int(math.ceil(base))
    m = cal.mean_wow(region, start, length)
    return int(math.ceil(base * (1.0 + sensitivity * TIER_FACTOR[tier] * m)))


def sail_days(cal: Calendar, rig: dict, frm: dict | None, to: dict, depart: int,
              same_installation_move_days: int = 1) -> int:
    """frm/to: dict with keys well_id, installation_id, lat, lon, region."""
    if frm is not None and frm.get("well_id") is not None and frm["well_id"] == to["well_id"]:
        return 0
    if (frm is not None and frm.get("installation_id") and to.get("installation_id")
            and frm["installation_id"] == to["installation_id"]):
        base = float(same_installation_move_days)
    else:
        nm = great_circle_nm(frm["lat"], frm["lon"], to["lat"], to["lon"])
        base = nm / (rig["transit_speed_knots"] * 24.0) + rig["mob_demob_days"]
    mult = 1.0 + rig["move_sensitivity"] * cal.wow(to["region"], depart)
    return int(math.ceil(base * mult))


def compatible(rig: dict, well: dict) -> bool:
    """Spec §5.1.  rig['capabilities'] / well['required_capabilities'] are sets;
    allowed_rig_types / allowed_rigs / excluded_rigs are sets (empty = no limit)."""
    if not (rig["min_water_depth_m"] <= well["water_depth_m"] <= rig["max_water_depth_m"]):
        return False
    if well["measured_depth_m"] > rig["max_drilling_depth_m"]:
        return False
    if not well["required_capabilities"] <= rig["capabilities"]:
        return False
    if well["allowed_rig_types"] and rig["rig_type"] not in well["allowed_rig_types"]:
        return False
    if well["allowed_rigs"] and rig["rig_id"] not in well["allowed_rigs"]:
        return False
    if rig["rig_id"] in well["excluded_rigs"]:
        return False
    return True


def split_set(v) -> set:
    if v is None or (isinstance(v, float) and math.isnan(v)) or v == "":
        return set()
    return set(str(v).split(";"))
