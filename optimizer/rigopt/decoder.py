"""Problem definition and schedule decoder.

A *task* is the unit the optimizer sequences on a rig:

* ``W``  a whole (unphased) well
* ``C``  a conductor campaign: the top holes of all its wells, consecutively (spec §5.8)
* ``M``  the main phase of one phased well (any compatible rig; after its campaign)

A solution is one ordered task list per rig.  ``decode`` places the tasks of all
rigs in (approximately) chronological order, each at the earliest day that
respects every hard rule (spec §5): sailing, rig contract and unavailability,
frozen period, time windows, precedence, one rig per installation, strategic
windows, horizon.  A task that cannot be placed is dropped with a reason.  The
objective (spec §8) and penalties for soft rules are computed on the result.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .data import DAY_OFFSET, Model
from .economics import Economics

BIG_MANDATORY = 5.0e9     # per missing mandatory / strategic / required well
BIG_CAMPAIGN = 5.0e9      # campaign with top holes but a missing main phase
TARGET_PENALTY = 3.0e8    # per exploration well below min / above max in a year
FREEDOM_PENALTY = 1.0e9   # per well over max_wells_changed


@dataclass
class Task:
    tid: int
    kind: str                 # W | C | M
    key: str                  # well id or campaign id (+ ":main" for M)
    wells: list               # well indices
    segs: list                # [(well idx, phase)]
    compat: frozenset         # rig indices
    es: int
    ls: int | None
    mcb: dict                 # well idx -> must-complete-by / strategic window end (day)
    deps: list = field(default_factory=list)   # [(well idx, lag)] well must be finished
    camp_dep: int | None = None                # M: campaign task id
    camp_lag: int = 0
    required: bool = False
    insertable: bool = True
    ref: tuple | None = None                   # (rig idx, start) in the reference schedule
    final_for: list = field(default_factory=list)  # wells finished when this task is placed


class Problem:
    """Model + run settings -> tasks, frozen part, reference, constants for decoding."""

    def __init__(self, m: Model, econ: Economics):
        self.m, self.e, s = m, econ, m.s
        self.R = len(m.rigs)
        rig_ix = {r.rig_id: r.idx for r in m.rigs}
        self.rig_ix = rig_ix
        F = m.frozen_df
        frozen_keys = set(zip(F.well_id, F.phase))
        self.frozen_wells_done = {}       # well idx -> end of its last frozen segment if fully frozen
        self.frozen_segs = []             # (rig, wi, phase, s, e, base, sail)
        for a in F.itertuples():
            wi = m.well_by_id[a.well_id].idx
            self.frozen_segs.append((rig_ix[a.rig_id], wi, a.phase, int(a.s), int(a.e), float(a.base_duration_days),
                                     int(a.sail_days_in)))
        # Frozen dates are fixed, but report their sailing with the current rule (any time saved is idle).
        fz = []
        for ri in range(len(m.rigs)):
            segs = sorted([x for x in self.frozen_segs if x[0] == ri], key=lambda x: x[3])
            for k, x in enumerate(segs):
                if k == 0:
                    fz.append(x)
                    continue
                p = segs[k - 1]
                need = m.sail_days(m.rigs[ri], m.loc_of_well(m.wells[p[1]]), m.loc_of_well(m.wells[x[1]]), p[4])
                fz.append(x[:6] + (min(need, max(0, x[3] - p[4])),))
        self.frozen_segs = fz
        # tasks
        tasks: list[Task] = []
        def wwin(w):
            es = max(w.es, m.freeze_day)
            ls = w.ls
            mcb = w.mcb
            if w.sw:
                es = max(es, w.sw[0])
                mcb = w.sw[1] if mcb is None else min(mcb, w.sw[1])
            return es, ls, mcb

        done_campaign = set()
        self.task_of_well = {}            # well idx -> final task id
        self.first_task_of_well = {}
        for w in m.wells:
            if w.status in ("cancelled", "completed"):
                continue
            ph_frozen = {ph for (wid, ph) in frozen_keys if wid == w.well_id}
            if w.phased:
                if "main" in ph_frozen or (not w.phases and ph_frozen):
                    continue
                cid = w.campaign
                if cid not in done_campaign and "top_hole" not in ph_frozen:
                    done_campaign.add(cid)
                    ws = m.campaigns[cid]["wells"]
                    wins = [wwin(m.wells[i]) for i in ws]
                    compat = frozenset.intersection(*[frozenset(rig_ix[x] for x in m.wells[i].compat) for i in ws])
                    lsl = [x[1] for x in wins if x[1] is not None]
                    deps = [p for i in ws for p in m.wells[i].preds]
                    t = Task(len(tasks), "C", cid, list(ws), [(i, "top_hole") for i in ws], compat,
                             max(x[0] for x in wins), min(lsl) if lsl else None, {}, deps)
                    tasks.append(t)
                    for i in ws:
                        self.first_task_of_well[i] = t.tid
                camp_tid = next((t.tid for t in tasks if t.kind == "C" and t.key == cid), None)
                es, ls, mcb = wwin(w)
                t = Task(len(tasks), "M", w.well_id + ":main", [w.idx], [(w.idx, "main")],
                         frozenset(rig_ix[x] for x in w.compat), m.freeze_day, None,
                         {w.idx: mcb} if mcb is not None else {}, [], camp_tid, w.phases["main"]["min_lag"])
                t.final_for = [w.idx]
                tasks.append(t)
                self.task_of_well[w.idx] = t.tid
                if camp_tid is None:   # top hole frozen: main still to schedule
                    self.first_task_of_well.setdefault(w.idx, t.tid)
                continue
            if ph_frozen:
                continue
            es, ls, mcb = wwin(w)
            t = Task(len(tasks), "W", w.well_id, [w.idx], [(w.idx, "full")], frozenset(rig_ix[x] for x in w.compat),
                     es, ls, {w.idx: mcb} if mcb is not None else {}, list(w.preds))
            t.final_for = [w.idx]
            t.required = w.mandatory
            tasks.append(t)
            self.task_of_well[w.idx] = t.tid
            self.first_task_of_well[w.idx] = t.tid
        for t in tasks:
            if t.kind == "C":
                t.required = any(m.wells[i].mandatory for i in t.wells)
        self.tasks = tasks
        self.T = len(tasks)

        # frozen state: well ends, installation intervals, rig start states
        seg_end = {}
        for (r, wi, ph, a, b, base, sail) in self.frozen_segs:
            seg_end.setdefault(wi, []).append((ph, b))
        for wi, lst in seg_end.items():
            w = m.wells[wi]
            if not w.phased or any(ph in ("main", "full") for ph, _ in lst):
                self.frozen_wells_done[wi] = max(b for _, b in lst)
        self.frozen_campaign_end = {}
        for cid, c in m.campaigns.items():
            ends = [b for (r, wi, ph, a, b, *_ ) in self.frozen_segs if ph == "top_hole" and wi in c["wells"]]
            if ends:
                self.frozen_campaign_end[cid] = (max(ends), next(r for (r, wi, ph, *_ ) in self.frozen_segs
                                                                 if ph == "top_hole" and wi in c["wells"]))
        self.rig_start = []
        for r in m.rigs:
            segs = sorted([x for x in self.frozen_segs if x[0] == r.idx], key=lambda x: x[3])
            if segs:
                last = segs[-1]
                self.rig_start.append((last[4], m.loc_of_well(m.wells[last[1]])))
            else:
                self.rig_start.append((max(0, r.cs), m.initial_loc(r)))
        self.inst_frozen = {}
        for (r, wi, ph, a, b, *_ ) in self.frozen_segs:
            self.inst_frozen.setdefault(m.wells[wi].inst_key, []).append((a, b, r))

        # per-segment constants
        self.seg_info = {}
        for t in tasks:
            for wi, ph in t.segs:
                w = m.wells[wi]
                if ph == "full":
                    base, sens = w.base, w.sens
                else:
                    base, sens = w.phases[ph]["base"], w.phases[ph]["sens"]
                self.seg_info[(wi, ph)] = (base, sens, s.tier_factor[w.tier], w.region, m.loc_of_well(w),
                                           w.inst_key, s.winter_penalty_share[w.tier] * w.rig_day_cost)
        self.unavail = [[(a, b) for a, b, _ in r.unavail] for r in m.rigs]
        self.targets = m.targets
        self.ref_seqs = None
        self.ref_tasks = {}
        self._apply_reference_and_freedom()

    # ------------------------------------------------------------------ reference + optimization freedom
    def _apply_reference_and_freedom(self):
        m, s = self.m, self.m.s
        ref = m.reference_df
        ref = ref[ref.s >= m.freeze_day] if len(ref) else ref
        key_tid = {}
        for t in self.tasks:
            key_tid[t.key] = t.tid
        seqs = [[] for _ in range(self.R)]
        seen = set()
        for a in ref.sort_values(["rig_id", "s"]).itertuples():
            w = m.well_by_id[a.well_id]
            if a.phase == "top_hole":
                key = w.campaign
            elif a.phase == "main":
                key = a.well_id + ":main"
            else:
                key = a.well_id
            tid = key_tid.get(key)
            if tid is None or tid in seen:
                continue
            seen.add(tid)
            r = self.rig_ix[a.rig_id]
            seqs[r].append(tid)
            self.tasks[tid].ref = (r, int(a.s))
            self.ref_tasks[tid] = (r, int(a.s))
        self.ref_seqs = seqs
        locked = set(s.locked_wells)
        for t in self.tasks:
            is_locked = any(m.wells[i].well_id in locked for i in t.wells)
            if t.ref is not None:
                r0, s0 = t.ref
                if s.max_shift_days is not None:
                    t.es = max(t.es, s0 - s.max_shift_days)
                    t.ls = s0 + s.max_shift_days if t.ls is None else min(t.ls, s0 + s.max_shift_days)
                if not s.allow_rig_change:
                    t.compat = t.compat & frozenset([r0])
                if not s.allow_add_remove_wells:
                    t.required = True
                if is_locked:
                    t.es = max(t.es, s0)
                    t.ls = s0 if t.ls is None else min(t.ls, s0)
                    t.compat = t.compat & frozenset([r0])
                    t.required = True
            else:
                if not s.allow_add_remove_wells or is_locked:
                    t.insertable = t.required  # mandatory wells may always be added

    def seqs_from_df(self, df):
        """Per-rig task sequences from a schedule table (well_id, phase, rig_id, start_date)."""
        m = self.m
        key_tid = {t.key: t.tid for t in self.tasks}
        df = df.copy()
        df["s"] = df.start_date.map(m.day)
        seqs = [[] for _ in range(self.R)]
        seen = set()
        for a in df.sort_values(["s", "rig_id"]).itertuples():
            if a.well_id not in m.well_by_id or a.rig_id not in self.rig_ix:
                continue
            w = m.well_by_id[a.well_id]
            key = w.campaign if a.phase == "top_hole" else (a.well_id + ":main" if a.phase == "main" else a.well_id)
            tid = key_tid.get(key)
            if tid is None or tid in seen:
                continue
            seen.add(tid)
            seqs[self.rig_ix[a.rig_id]].append(tid)
        return seqs

    # ------------------------------------------------------------------ helpers
    def task_wells_final(self, t: Task):
        return t.final_for if t.kind != "C" else []


@dataclass
class Result:
    placed: dict            # tid -> (rig, [ (wi, phase, s, e, base, sail) ])
    dropped: dict           # tid -> reason
    well_seg: dict          # wi -> {phase: (rig, s, e, base, sail)}
    objective: float
    parts: dict
    well_value: dict        # wi -> value
    expl_count: dict        # year -> count


def decode(P: Problem, seqs: list, dur_override: dict | None = None, relax: bool = False,
           wow_scale: dict | None = None, pad: dict | None = None) -> Result:
    """Place tasks; dur_override: (wi, phase) -> base days (Monte Carlo / discovery risk).
    relax=True ignores latest-start / must-complete windows (used for reporting re-runs)."""
    m, E, s = P.m, P.e, P.m.s
    if pad is None:
        pad = getattr(P, "pad", None)
    not_before = getattr(P, "not_before", None) or {}
    tasks = P.tasks
    R = P.R
    H = m.H
    freeze = m.freeze_day
    wow_cum = m.wow_cum
    wow_day = m.wow_day
    rigs = m.rigs
    status = [0] * P.T                        # 0 out, 1 pending, 2 placed, 3 dropped
    for q in seqs:
        for t in q:
            status[t] = 1
    well_end = dict(P.frozen_wells_done)
    camp_end = {}                              # campaign task id -> (end, rig)
    inst_busy = {k: list(v) for k, v in P.inst_frozen.items()}
    free = [x[0] for x in P.rig_start]
    loc = [x[1] for x in P.rig_start]
    ptr = [0] * R
    placed, dropped = {}, {}
    final_status = {}
    for t in tasks:
        for wi in t.final_for:
            final_status[wi] = t.tid
    seg_info = P.seg_info
    sim = s.same_installation_move_days

    sail = m.sail_days   # single sailing rule shared with reports (data.Model.sail_days)

    def readiness(t: Task):
        for wi, lag in t.deps:
            if wi in well_end:
                continue
            ft = final_status.get(wi)
            if ft is not None and status[ft] == 1:
                return 1
            return 2
        if t.kind == "M":
            if t.camp_dep is None:
                return 0
            st = status[t.camp_dep]
            if st == 2:
                return 0
            return 1 if st == 1 else 2
        return 0

    def place(t: Task, ri: int):
        r = rigs[ri]
        depart = free[ri]
        first = seg_info[t.segs[0]][4]
        sail0 = sail(r, loc[ri], first, depart)
        st = max(depart + sail0 + (pad.get(t.tid, 0) if pad else 0), t.es, freeze, r.cs, not_before.get(t.tid, 0))
        for wi, lag in t.deps:
            st = max(st, well_end[wi] + lag)
        if t.kind == "M" and t.camp_dep is not None:
            st = max(st, camp_end[t.camp_dep][0] + t.camp_lag)
        elif t.kind == "M":
            cid = m.wells[t.wells[0]].campaign
            if cid in P.frozen_campaign_end:
                st = max(st, P.frozen_campaign_end[cid][0] + t.camp_lag)
        unav = P.unavail[ri]
        for _ in range(400):
            segs = []
            tt = st
            prev = None
            for (wi, ph) in t.segs:
                base, sens, tf, region, lc, ik, pen = seg_info[(wi, ph)]
                if dur_override is not None and (wi, ph) in dur_override:
                    base = dur_override[(wi, ph)]
                if prev is None:
                    mv = sail0
                else:
                    mv = sail(r, prev, lc, tt)
                    tt += mv
                L = int(math.ceil(base))
                a = tt + DAY_OFFSET
                c = wow_cum[region]
                mw = (c[a + max(1, L)] - c[a]) / max(1, L)
                if wow_scale is not None:
                    mw *= wow_scale.get(region, 1.0)
                eff = int(math.ceil(base * (1.0 + sens * tf * mw)))
                segs.append((wi, ph, tt, tt + eff, base, mv))
                tt += eff
                prev = lc
            shift = -1
            for (us, ue) in unav:
                for g in segs:
                    if g[2] < ue and us < g[3]:
                        if ue > shift:
                            shift = ue
            if shift < 0:
                for g in segs:
                    ik = seg_info[(g[0], g[1])][5]
                    lst = inst_busy.get(ik)
                    if lst:
                        for (a, b, rr) in lst:
                            if rr != ri and a < g[3] and g[2] < b and b > shift:
                                shift = b
            if shift < 0:
                break
            st = max(shift, st + 1)
            if st >= H:
                return None, "no_capacity_in_horizon"
        else:
            return None, "no_capacity_in_horizon"
        if segs[-1][3] > r.ce or segs[0][2] >= H or any(g[2] >= H for g in segs):
            return None, "no_capacity_in_horizon"
        if not relax:
            if t.ls is not None and segs[0][2] > t.ls:
                return None, "time_window_infeasible"
            for wi, dl in t.mcb.items():
                if max(g[3] for g in segs if g[0] == wi) > dl:
                    return None, "time_window_infeasible"
        return segs, None

    n_left = sum(len(q) for q in seqs)
    while n_left > 0:
        best, waiting = -1, []
        for ri in range(R):
            q = seqs[ri]
            while ptr[ri] < len(q):
                t = tasks[q[ptr[ri]]]
                rd = readiness(t)
                if rd == 2:
                    status[t.tid] = 3
                    dropped[t.tid] = "precedence_blocked"
                    ptr[ri] += 1
                    n_left -= 1
                    continue
                break
            if ptr[ri] >= len(q):
                continue
            if rd == 0:
                if best < 0 or free[ri] < free[best]:
                    best = ri
            else:
                waiting.append(ri)
        if best < 0:
            if not waiting:
                break
            ri = min(waiting, key=lambda x: (free[x], x))
            tid = seqs[ri][ptr[ri]]
            status[tid] = 3
            dropped[tid] = "precedence_blocked"
            ptr[ri] += 1
            n_left -= 1
            continue
        ri = best
        tid = seqs[ri][ptr[ri]]
        t = tasks[tid]
        ptr[ri] += 1
        n_left -= 1
        if ri not in t.compat:
            status[tid] = 3
            dropped[tid] = "no_compatible_rig"
            continue
        segs, why = place(t, ri)
        if segs is None:
            status[tid] = 3
            dropped[tid] = why
            continue
        status[tid] = 2
        placed[tid] = (ri, segs)
        free[ri] = segs[-1][3]
        loc[ri] = seg_info[(segs[-1][0], segs[-1][1])][4]
        for g in segs:
            inst_busy.setdefault(seg_info[(g[0], g[1])][5], []).append((g[2], g[3], ri))
        if t.kind == "C":
            camp_end[tid] = (segs[-1][3], ri)
        for wi in t.final_for:
            well_end[wi] = segs[-1][3]

    return evaluate(P, placed, dropped)


def evaluate(P: Problem, placed: dict, dropped: dict) -> Result:
    m, E, s = P.m, P.e, P.m.s
    tasks = P.tasks
    well_seg = {}
    for (r, wi, ph, a, b, base, sail) in P.frozen_segs:
        well_seg.setdefault(wi, {})[ph] = (r, a, b, base, sail, True)
    sail_days = 0
    idle_days = 0
    winter_pen = 0.0
    for tid, (ri, segs) in placed.items():
        for (wi, ph, a, b, base, mv) in segs:
            well_seg.setdefault(wi, {})[ph] = (ri, a, b, base, mv, False)
            sail_days += mv
            pen = P.seg_info[(wi, ph)][6]
            if pen:
                winter_pen += pen * max(0.0, (b - a) - base)
    # idle: gaps on each rig beyond sailing, between consecutive optimized assignments
    by_rig = {}
    for tid, (ri, segs) in placed.items():
        by_rig.setdefault(ri, []).append((segs[0][2] - segs[0][5], segs[-1][3]))
    for ri, lst in by_rig.items():
        lst.sort()
        prev = max(P.rig_start[ri][0], m.freeze_day)
        for a, b in lst:
            idle_days += max(0, a - prev)
            prev = b
    # values of complete wells
    values = {}
    incomplete = 0
    batch_pen = 0.0
    for wi, segd in well_seg.items():
        w = m.wells[wi]
        if w.phased:
            if "top_hole" not in segd or "main" not in segd:
                incomplete += 1
                continue
            th, mn = segd["top_hole"], segd["main"]
            values[wi] = E.value(wi, {"top_hole": th[1], "main": mn[1]}, mn[2])
            if not (th[5] and mn[5]):
                batch_pen += s.batch_gap_penalty_per_day * max(0, mn[1] - th[2])
                if mn[0] == th[0]:
                    batch_pen -= s.batch_same_rig_bonus
        else:
            g = segd.get("full")
            if g is None:
                continue
            values[wi] = E.value(wi, {"full": g[1]}, g[2])
    # exploration targets
    expl = {}
    for wi in values:
        w = m.wells[wi]
        if w.is_expl:
            segd = well_seg[wi]
            st = min(x[1] for x in segd.values())
            y = m.date(st).year
            expl[y] = expl.get(y, 0) + 1
    tgt_pen = 0.0
    tgt_dev = 0
    for y, (lo, hi) in P.targets.items():
        n = expl.get(y, 0)
        dev = max(0, lo - n) + max(0, n - hi)
        tgt_dev += dev
    tgt_pen = TARGET_PENALTY * tgt_dev
    # mandatory / required
    missing = 0
    for t in tasks:
        if t.required and t.tid not in placed:
            missing += 1
    # freedom
    changed = 0
    shift_pen = 0.0
    if P.ref_tasks:
        for tid, (r0, s0) in P.ref_tasks.items():
            if tid in placed:
                ri, segs = placed[tid]
                d = abs(segs[0][2] - s0)
                if ri != r0 or d > s.change_tolerance_days:
                    changed += 1
                shift_pen += s.change_penalty_weight * (d + (30 if ri != r0 else 0))
            else:
                changed += 1
        for tid in placed:
            if tid not in P.ref_tasks:
                changed += 1
    free_pen = 0.0
    if s.max_wells_changed is not None and changed > s.max_wells_changed:
        free_pen = FREEDOM_PENALTY * (changed - s.max_wells_changed)
    value = sum(values.values())
    obj = (value - winter_pen - batch_pen - s.sail_day_weight * sail_days - s.idle_day_weight * idle_days
           - tgt_pen - BIG_MANDATORY * missing - BIG_CAMPAIGN * incomplete - free_pen - shift_pen)
    parts = dict(value=value, winter_penalty=winter_pen, batch_penalty=batch_pen, sail_days=sail_days,
                 idle_days=idle_days, target_deviation=tgt_dev, missing_required=missing,
                 incomplete_campaign_wells=incomplete, changed_tasks=changed, freedom_penalty=free_pen,
                 change_penalty=shift_pen)
    return Result(placed, dropped, well_seg, round(obj, 0), parts, values, expl)
