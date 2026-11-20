"""'Manual-style' first-come-first-served baseline schedule.

Mimics how a schedule tends to be built by hand today:

* each well is *ready* on its earliest start date (the data has no 'created' date; wells of
  an asset share readiness, so earliest start is used as the proxy);
* when a rig becomes free it takes the next ready well, preferring the field it is already
  working in (field by field), otherwise the earliest-ready well it can drill;
* a conductor campaign is followed straight away by its main phases on the same rig;
* some whitespace is left between wells (random 3-14 days before about a third of the wells);
* wells with a clearly negative value are left out (planners know them), but no attempt is
  made to optimize value, weather or exploration targets.

Seeded, so it is reproducible.
"""
from __future__ import annotations

import random

from .decoder import Problem, decode

READY_SLACK = 30          # a well counts as ready if it is ready within this many days of the rig being free
FIELD_STICKY = 120        # stay in the same field if a well there is ready within this many days
WHITESPACE_P = 0.35       # share of wells preceded by some slack
WHITESPACE_DAYS = (3, 14)


def build_baseline(P: Problem, seed: int = 11):
    m, e, rng = P.m, P.e, random.Random(seed)
    camp_mains = {}
    for t in P.tasks:
        if t.kind == "M" and t.camp_dep is not None:
            camp_mains.setdefault(t.camp_dep, []).append(t.tid)
    for c, lst in camp_mains.items():
        lst.sort(key=lambda tid: -P.tasks[c].wells.index(P.tasks[tid].wells[0]))

    def field_of(t):
        w = m.wells[t.wells[0]]
        return w.field_id or ("EXPL:" + w.well_id)

    def worth(t):
        if t.required:
            return True
        return sum(e.value_if_ideal(i, max(t.es, m.freeze_day)) for i in t.wells) > 0

    todo = [t for t in P.tasks if t.kind != "M" and t.compat and worth(t)]
    # planners drill the injectors / predecessors that wanted wells depend on
    want = {t.tid for t in todo}
    changed = True
    while changed:
        changed = False
        for t in list(todo):
            for wi, _ in t.deps:
                pt = P.task_of_well.get(wi)
                if pt is not None and pt not in want and P.tasks[pt].compat and P.tasks[pt].kind != "M":
                    want.add(pt)
                    todo.append(P.tasks[pt])
                    changed = True
    todo.sort(key=lambda t: (t.es, t.key))
    seqs = [[] for _ in range(P.R)]
    pad = {}
    P.pad = pad
    res = decode(P, seqs)
    placed_wells = set(P.frozen_wells_done)
    guard = 0
    dropped_log = []
    tried = set()
    P.baseline_dropped = dropped_log
    while todo and guard < 20000:
        guard += 1
        # rig free times from the current decode
        free = []
        for ri in range(P.R):
            last = [res.placed[t][1][-1][3] for t in seqs[ri] if t in res.placed]
            f = max(last) if last else max(P.rig_start[ri][0], m.freeze_day)
            if f < min(m.H, m.rigs[ri].ce):
                free.append((f, ri))
        if not free:
            break
        free.sort()
        progressed = False
        for f, ri in free:
            cands = [t for t in todo if ri in t.compat and (t.tid, ri) not in tried
                     and all(wi in placed_wells for wi, _ in t.deps)]
            if not cands:
                continue
            last_field = None
            if seqs[ri]:
                last_field = field_of(P.tasks[seqs[ri][-1]])
            elif P.rig_start[ri][1][0] in m.well_by_id:
                last_field = m.well_by_id[P.rig_start[ri][1][0]].field_id
            urgent = [t for t in cands if t.required and t.es <= f + 90]
            if urgent:  # planners protect mandatory / strategic wells and their windows
                t = min(urgent, key=lambda t: (t.ls if t.ls is not None else 10 ** 9, t.es))
            else:
                same = [t for t in cands if field_of(t) == last_field and t.es <= f + FIELD_STICKY]
                ready = [t for t in cands if t.es <= f + READY_SLACK]
                if same:
                    pool = same
                elif ready:
                    pool = ready
                else:
                    pool = [min(cands, key=lambda t: t.es)]
                first = min(t.es for t in pool)
                pool = [t for t in pool if t.es <= first + 15]
                t = rng.choice(pool)
            grp = [t.tid] + camp_mains.get(t.tid, [])
            seqs[ri].extend(grp)
            if not urgent and rng.random() < WHITESPACE_P:
                pad[t.tid] = rng.randint(*WHITESPACE_DAYS)
            todo.remove(t)
            res = decode(P, seqs)
            if any(g in res.dropped for g in grp):
                why = res.dropped.get(t.tid) or next(res.dropped[g] for g in grp if g in res.dropped)
                for g in grp:
                    seqs[ri].remove(g)
                pad.pop(t.tid, None)
                tried.add((t.tid, ri))
                if why == "no_capacity_in_horizon" or why == "time_window_infeasible":
                    todo.append(t)        # another rig may still fit it
                else:
                    dropped_log.append((t.key, why))
                res = decode(P, seqs)
            placed_wells.update(res.well_value)
            progressed = True
            break
        if not progressed:
            break
    P.baseline_left = [t.key for t in todo]
    return seqs, pad, res
