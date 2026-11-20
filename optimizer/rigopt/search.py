"""Seeded simulated annealing over per-rig task sequences.

Deterministic: the same input, settings and seed give the same output (spec §13.10).
The search starts from the reference schedule (spec §8.5), repairs it (mandatory
wells, exploration targets), then applies random neighbourhood moves, each fully
re-decoded.  The best solution found is returned.
"""
from __future__ import annotations

import math
import random

from .decoder import Problem, Result, decode


def default_iterations(P: Problem) -> int:
    n = P.T
    if n < 60:
        return 15000
    if n < 400:
        return 60000
    return 25000


class Search:
    def __init__(self, P: Problem, seed: int = 42, log=None):
        self.P = P
        self.rng = random.Random(seed)
        self.log = log or (lambda *a: None)
        self.camp_mains = {}
        for t in P.tasks:
            if t.kind == "M" and t.camp_dep is not None:
                self.camp_mains.setdefault(t.camp_dep, []).append(t.tid)
        # main phase order inside a campaign: last well first (expected pattern, spec §5.8)
        for c, lst in self.camp_mains.items():
            lst.sort(key=lambda tid: -P.tasks[c].wells.index(P.tasks[tid].wells[0]))

    # ------------------------------------------------------------------ helpers
    def evaluate(self, seqs) -> Result:
        return decode(self.P, seqs)

    @staticmethod
    def copy(seqs):
        return [list(q) for q in seqs]

    def in_solution(self, seqs):
        return {t for q in seqs for t in q}

    def clean(self, seqs, res: Result):
        """Remove dropped tasks so sequences only hold placed tasks."""
        if not res.dropped:
            return seqs
        return [[t for t in q if t not in res.dropped] for q in seqs]

    def group(self, tid):
        """Tasks that must enter / leave the solution together (a campaign and its main phases)."""
        t = self.P.tasks[tid]
        if t.kind == "C":
            return [tid] + self.camp_mains.get(tid, [])
        if t.kind == "M" and t.camp_dep is not None:
            return [t.camp_dep] + self.camp_mains.get(t.camp_dep, [])
        return [tid]

    def missing_preds(self, tid, sol):
        """Insertable predecessor tasks (recursively) of `tid` that are not in the solution, earliest first."""
        P = self.P
        out, stack, seen = [], [tid], set()
        while stack:
            t = P.tasks[stack.pop()]
            for wi, _lag in t.deps:
                pt = P.task_of_well.get(wi)
                if pt is None or pt in sol or pt in seen or wi in P.frozen_wells_done:
                    continue
                pt = P.tasks[pt]
                if not pt.insertable or not pt.compat or pt.kind != "W":
                    continue
                seen.add(pt.tid)
                out.append(pt.tid)
                stack.append(pt.tid)
        return out[::-1]

    def insert_with_preds(self, seqs, tid, ri, pos, sol):
        """Insert `tid` at (ri, pos), its missing predecessors just before it (same rig if compatible)."""
        P = self.P
        for pt in self.missing_preds(tid, sol):
            t = P.tasks[pt]
            rj = ri if ri in t.compat else self.rng.choice(sorted(t.compat))
            if rj == ri:
                seqs[ri].insert(pos, pt)
                pos += 1
            else:
                seqs[rj].insert(min(len(seqs[rj]), pos), pt)
        self.insert_group(seqs, tid, ri, pos)

    def time_pos(self, seq, res: Result, day: int) -> int:
        """Index in `seq` of the first task starting after `day`."""
        for i, t in enumerate(seq):
            pl = res.placed.get(t)
            if pl is not None and pl[1][0][2] > day:
                return i
        return len(seq)

    def insert_group(self, seqs, tid, ri, pos):
        grp = self.group(tid)
        q = seqs[ri]
        for k, g in enumerate(grp):
            q.insert(min(pos + k, len(q)), g)

    # ------------------------------------------------------------------ best insertion (repair)
    def best_insert(self, seqs, tid, res: Result, window=None):
        P = self.P
        t = P.tasks[tid]
        best = None
        rigs = sorted(t.compat)
        for ri in rigs:
            n = len(seqs[ri])
            if window is None:
                cand = range(n + 1)
            else:
                c = self.time_pos(seqs[ri], res, t.es)
                cand = range(max(0, c - window), min(n, c + window) + 1)
            sol = self.in_solution(seqs)
            for pos in cand:
                s2 = self.copy(seqs)
                self.insert_with_preds(s2, tid, ri, pos, sol)
                r2 = self.evaluate(s2)
                if any(g in r2.dropped for g in self.group(tid)):
                    continue
                if best is None or r2.objective > best[0]:
                    best = (r2.objective, s2, r2)
        return best

    def repair(self, seqs, res):
        P = self.P
        sol = self.in_solution(seqs)
        for t in P.tasks:
            if t.required and t.tid not in res.placed and t.insertable and t.compat:
                if t.tid in sol:
                    seqs = [[x for x in q if x not in self.group(t.tid)] for q in seqs]
                    res = self.evaluate(seqs)
                b = self.best_insert(seqs, t.tid, res)
                if b is not None:
                    seqs, res = self.clean(b[1], b[2]), b[2]
                    res = self.evaluate(seqs)
                    self.log(f"  repair: inserted required {t.key} -> obj {res.objective/1e6:,.1f} M")
                sol = self.in_solution(seqs)
        # exploration targets: insert the best exploration wells into short years
        m = P.m
        skip = set()
        for _ in range(80):
            short = [y for y, (lo, hi) in sorted(P.targets.items()) if res.expl_count.get(y, 0) < lo and y not in skip]
            if not short:
                break
            y = short[0]
            y0, y1 = m.day(f"{y}-01-01"), m.day(f"{y}-12-31")
            sol = self.in_solution(seqs)
            cands = [t for t in P.tasks if t.tid not in sol and t.insertable and t.compat and t.kind == "W"
                     and m.wells[t.wells[0]].is_expl and t.es <= y1 and (t.ls is None or t.ls >= y0)]
            cands.sort(key=lambda t: -P.e.value_if_ideal(t.wells[0], max(t.es, y0)))
            best = None
            nb = P.__dict__.setdefault("not_before", {})
            for t in cands[:8]:
                day = max(t.es, y0 + 120)
                had = nb.get(t.tid)
                nb[t.tid] = y0          # spud inside the short year: wait for it if the rig is free earlier
                for ri in sorted(t.compat):
                    c = self.time_pos(seqs[ri], res, day)
                    for pos in range(max(0, c - 1), min(len(seqs[ri]), c + 1) + 1):
                        s2 = self.copy(seqs)
                        self.insert_group(s2, t.tid, ri, pos)
                        r2 = self.evaluate(s2)
                        if t.tid in r2.dropped:
                            continue
                        if best is None or r2.objective > best[0]:
                            best = (r2.objective, s2, r2, t.tid)
                if had is None:
                    nb.pop(t.tid, None)
                else:
                    nb[t.tid] = had
            if best is None or best[0] <= res.objective:
                skip.add(y)
                continue
            nb[best[3]] = y0
            seqs, res = self.clean(best[1], best[2]), best[2]
            res = self.evaluate(seqs)
            self.log(f"  repair: exploration target {y} -> obj {res.objective/1e6:,.1f} M, deviation "
                     f"{res.parts['target_deviation']}")
        return seqs, res

    # ------------------------------------------------------------------ neighbourhood
    def neighbour(self, seqs, res: Result):
        P, rng = self.P, self.rng
        s2 = self.copy(seqs)
        nonempty = [i for i, q in enumerate(s2) if q]
        mv = rng.random()
        if mv < 0.05:                                   # main phase back onto its campaign rig
            return self.main_to_campaign_rig(seqs, res, random_pick=True)
        mv = rng.random()
        sol = None
        if mv < 0.22 and nonempty:                      # relocate within rig
            ri = rng.choice(nonempty)
            q = s2[ri]
            if len(q) < 2:
                return None
            i = rng.randrange(len(q))
            j = max(0, min(len(q) - 1, i + rng.choice([-1, 1]) * rng.randint(1, 6)))
            t = q.pop(i)
            q.insert(j, t)
        elif mv < 0.40 and nonempty:                    # swap within rig
            ri = rng.choice(nonempty)
            q = s2[ri]
            if len(q) < 2:
                return None
            i = rng.randrange(len(q))
            j = max(0, min(len(q) - 1, i + rng.choice([-1, 1]) * rng.randint(1, 5)))
            q[i], q[j] = q[j], q[i]
        elif mv < 0.58 and nonempty:                    # move to another rig at a similar time
            ri = rng.choice(nonempty)
            q = s2[ri]
            i = rng.randrange(len(q))
            t = P.tasks[q[i]]
            others = [r for r in t.compat if r != ri]
            if not others:
                return None
            rj = rng.choice(sorted(others))
            day = res.placed[q[i]][1][0][2] if q[i] in res.placed else t.es
            q.pop(i)
            pos = self.time_pos(s2[rj], res, day) + rng.randint(-2, 2)
            s2[rj].insert(max(0, min(len(s2[rj]), pos)), t.tid)
        elif mv < 0.70 and nonempty:                    # swap across rigs
            ri = rng.choice(nonempty)
            i = rng.randrange(len(s2[ri]))
            a = P.tasks[s2[ri][i]]
            others = [r for r in a.compat if r != ri and s2[r]]
            if not others:
                return None
            rj = rng.choice(sorted(others))
            day = res.placed[a.tid][1][0][2] if a.tid in res.placed else a.es
            j = min(len(s2[rj]) - 1, max(0, self.time_pos(s2[rj], res, day) + rng.randint(-2, 1)))
            b = P.tasks[s2[rj][j]]
            if ri not in b.compat:
                return None
            s2[ri][i], s2[rj][j] = b.tid, a.tid
        elif mv < 0.76 and nonempty:                    # reverse a short segment
            ri = rng.choice(nonempty)
            q = s2[ri]
            if len(q) < 3:
                return None
            i = rng.randrange(len(q) - 2)
            j = min(len(q), i + rng.randint(2, 5))
            q[i:j] = q[i:j][::-1]
        elif mv < 0.92:                                 # insert from pool
            sol = self.in_solution(s2)
            short = any(res.expl_count.get(y, 0) < lo for y, (lo, hi) in P.targets.items())
            pool = [t for t in P.tasks if t.tid not in sol and t.insertable and t.compat
                    and t.kind != "M"]
            if not pool:
                return None
            day = None
            if short and rng.random() < 0.5:
                ys = [y for y, (lo, hi) in sorted(P.targets.items()) if res.expl_count.get(y, 0) < lo]
                y = rng.choice(ys)
                y0, y1 = P.m.day(f"{y}-01-01"), P.m.day(f"{y}-12-31")
                ex = [t for t in pool if P.m.wells[t.wells[0]].is_expl and t.es <= y1 and (t.ls is None or t.ls >= y0)]
                if ex:
                    pool = ex
                    day = y0 + rng.randint(0, 330)
            t = rng.choice(pool)
            ri = rng.choice(sorted(t.compat))
            if day is None:
                day = max(t.es, P.m.freeze_day) + rng.randint(0, 900)
            pos = self.time_pos(s2[ri], res, day)
            self.insert_with_preds(s2, t.tid, ri, pos, sol)
        else:                                           # remove an optional task
            cand = [t for q in s2 for t in q if not P.tasks[t].required]
            if not cand:
                return None
            grp = set(self.group(rng.choice(cand)))
            if any(P.tasks[g].required for g in grp):
                return None
            s2 = [[x for x in q if x not in grp] for q in s2]
        return s2

    # ------------------------------------------------------------------ main loop
    def run(self, init_seqs=None, iterations=None, t0=3.0e6, t1=2.0e4, extra_seeds=()):
        P = self.P
        seqs = self.copy(init_seqs if init_seqs is not None else P.ref_seqs)
        res = self.evaluate(seqs)
        seqs = self.clean(seqs, res)
        res = self.evaluate(seqs)
        self.log(f"  start: obj {res.objective/1e6:,.1f} M")
        seqs, res = self.repair(seqs, res)
        best_seqs, best = self.copy(seqs), res
        for alt in extra_seeds:   # e.g. a previous optimum (warm start)
            r = self.evaluate(alt)
            alt = self.clean(alt, r)
            r = self.evaluate(alt)
            if r.objective > best.objective:
                best_seqs, best, seqs, res = self.copy(alt), r, self.copy(alt), r
        n = iterations or default_iterations(P)
        cur_seqs, cur = seqs, res
        accepted = improved = 0
        for k in range(n):
            T = t0 * (t1 / t0) ** (k / max(1, n - 1))
            cand = self.neighbour(cur_seqs, cur)
            if cand is None:
                continue
            r = self.evaluate(cand)
            d = r.objective - cur.objective
            if d >= 0 or self.rng.random() < math.exp(d / T):
                cur_seqs, cur = self.clean(cand, r), r
                if r.dropped:
                    cur = self.evaluate(cur_seqs)
                accepted += 1
                if cur.objective > best.objective:
                    best_seqs, best = self.copy(cur_seqs), cur
                    improved += 1
            if (k + 1) % max(1, n // 10) == 0:
                self.log(f"  iter {k+1:>6}/{n}: current {cur.objective/1e6:,.1f} M, best {best.objective/1e6:,.1f} M")
        # final: restore targets / mandatory wells if needed, then polish
        best_seqs, best = self.repair(best_seqs, best)
        best_seqs, best = self.polish(best_seqs, best)
        return best_seqs, best

    def polish(self, seqs, res):
        P = self.P
        for _ in range(3):
            gained = False
            sol = self.in_solution(seqs)
            pool = [t for t in P.tasks if t.tid not in sol and t.insertable and t.compat and t.kind != "M"]
            pool = [t for t in pool if P.e.value_if_ideal(t.wells[0], max(t.es, P.m.freeze_day)) > 0]
            pool.sort(key=lambda t: -P.e.value_if_ideal(t.wells[0], max(t.es, P.m.freeze_day)))
            for t in pool[:40]:
                if t.tid in sol:
                    continue
                b = self.best_insert(seqs, t.tid, res, window=2)
                if b is not None and b[0] > res.objective:
                    seqs = self.clean(b[1], b[2])
                    res = self.evaluate(seqs)
                    sol = self.in_solution(seqs)
                    gained = True
            if not gained:
                break
        for _ in range(3):
            obj0 = res.objective
            seqs, res = self.campaign_mains_pass(seqs, res)
            seqs, res = self.idle_gap_pass(seqs, res)
            seqs, res = self.hill_climb(seqs, res)
            if res.objective <= obj0:
                break
        seqs, res = self.winter_swaps(seqs, res)
        return seqs, res

    # ------------------------------------------------------------------ campaign / idle-gap moves
    def _camp_rig_positions(self, seqs, c_tid):
        """(rig, positions) right after the campaign's top holes and after each of its main phases on that rig."""
        for ri, q in enumerate(seqs):
            if c_tid in q:
                i = q.index(c_tid)
                pos = [i + 1]
                mains = set(self.camp_mains.get(c_tid, []))
                j = i + 1
                while j < len(q) and q[j] in mains:
                    j += 1
                    pos.append(j)
                return ri, pos
        return None, []

    def main_to_campaign_rig(self, seqs, res, random_pick=False, tid=None):
        """Move one main phase onto the rig that drilled its campaign's top holes."""
        P, rng = self.P, self.rng
        if tid is None:
            cands = [t for q in seqs for t in q if P.tasks[t].kind == "M" and P.tasks[t].camp_dep is not None]
            if not cands:
                return None
            tid = rng.choice(cands)
        t = P.tasks[tid]
        ri, pos = self._camp_rig_positions(seqs, t.camp_dep)
        if ri is None or ri not in t.compat:
            return None
        s2 = self.copy(seqs)
        for q in s2:
            if tid in q:
                q.remove(tid)
        ri, pos = self._camp_rig_positions(s2, t.camp_dep)
        s2[ri].insert(rng.choice(pos) if random_pick else pos[-1], tid)
        return s2

    def campaign_mains_pass(self, seqs, res):
        """Deterministic: for every main phase, try each slot on its campaign rig right after the campaign."""
        P = self.P
        for c_tid, mains in self.camp_mains.items():
            for tid in mains:
                if not any(tid in q for q in seqs):
                    continue
                t = P.tasks[tid]
                base = self.copy(seqs)
                for q in base:
                    if tid in q:
                        q.remove(tid)
                ri, pos = self._camp_rig_positions(base, c_tid)
                if ri is None or ri not in t.compat:
                    continue
                best = None
                for p_ in pos:
                    s2 = self.copy(base)
                    s2[ri].insert(p_, tid)
                    r2 = self.evaluate(s2)
                    if not r2.dropped and r2.objective > res.objective and (best is None or r2.objective > best[0]):
                        best = (r2.objective, s2, r2)
                if best is not None:
                    seqs, res = best[1], best[2]
        return seqs, res

    def idle_gap_pass(self, seqs, res, min_gap=5):
        """A rig that waits because another rig occupies the installation it is about to work on:
        try giving the blocking task to the waiting rig, just before the task it waits for."""
        P, m = self.P, self.P.m
        changed = True
        rounds = 0
        while changed and rounds < 40:
            changed, rounds = False, rounds + 1
            for ri in range(len(seqs)):
                q = seqs[ri]
                for k in range(1, len(q)):
                    a, b = q[k - 1], q[k]
                    if a not in res.placed or b not in res.placed:
                        continue
                    a_end = res.placed[a][1][-1][3]
                    b_seg = res.placed[b][1][0]
                    gap = b_seg[2] - b_seg[5] - a_end
                    if gap < min_gap:
                        continue
                    ik = P.seg_info[(b_seg[0], b_seg[1])][5]
                    blockers = []
                    for tid, (rj, segs) in res.placed.items():
                        if rj == ri:
                            continue
                        if any(P.seg_info[(g[0], g[1])][5] == ik and g[2] < b_seg[2] and g[3] > a_end for g in segs):
                            blockers.append(tid)
                    best = None
                    for bt in blockers:
                        if ri not in P.tasks[bt].compat:
                            continue
                        s2 = self.copy(seqs)
                        for qq in s2:
                            if bt in qq:
                                qq.remove(bt)
                        s2[ri].insert(s2[ri].index(b), bt)
                        r2 = self.evaluate(s2)
                        if not r2.dropped and r2.objective > res.objective and (best is None or r2.objective > best[0]):
                            best = (r2.objective, s2, r2)
                    if best is not None:
                        seqs, res, changed = best[1], best[2], True
                        break
                if changed:
                    break
        return seqs, res

    def hill_climb(self, seqs, res, passes=2):
        """First-improvement pass over adjacent swaps on every rig."""
        for _ in range(passes):
            gained = False
            for ri in range(len(seqs)):
                i = 0
                while i < len(seqs[ri]) - 1:
                    s2 = self.copy(seqs)
                    s2[ri][i], s2[ri][i + 1] = s2[ri][i + 1], s2[ri][i]
                    r2 = self.evaluate(s2)
                    if r2.objective > res.objective and not r2.dropped:
                        seqs, res, gained = s2, r2, True
                    i += 1
            if not gained:
                break
        return seqs, res

    def winter_swaps(self, seqs, res):
        """Spec §13.13: swap a strongly_avoid well placed in winter with a similar-length
        'none' well on the same rig when that is feasible and improves the objective."""
        P, m = self.P, self.P.m
        def winter_share(tid):
            segs = res.placed[tid][1]
            a, b = segs[0][2], segs[-1][3]
            return sum(1 for d in range(a, b) if m.is_winter(d)) / max(1, b - a)
        for ri in range(len(seqs)):
            for tid in list(seqs[ri]):
                t = P.tasks[tid]
                if t.kind != "W" or tid not in res.placed:
                    continue
                w = m.wells[t.wells[0]]
                if w.tier != "strongly_avoid" or winter_share(tid) < 0.5:
                    continue
                best = None
                for oid in seqs[ri]:
                    o = P.tasks[oid]
                    if o.kind != "W" or oid == tid or ri not in o.compat:
                        continue
                    ow = m.wells[o.wells[0]]
                    if ow.tier != "none" or abs(ow.base - w.base) > 0.25 * w.base:
                        continue
                    s2 = self.copy(seqs)
                    i, j = s2[ri].index(tid), s2[ri].index(oid)
                    s2[ri][i], s2[ri][j] = oid, tid
                    r2 = self.evaluate(s2)
                    if not r2.dropped and r2.objective > res.objective and (best is None or r2.objective > best[0]):
                        best = (r2.objective, s2, r2)
                if best is not None:
                    seqs, res = best[1], best[2]
        return seqs, res
