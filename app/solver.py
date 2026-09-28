"""Core obligation accounting solver.

Pipeline for one assessment:

1. normalize the request (sort components/edges -> order invariance),
2. parse every SPDX-style expression        -> "invalid-expression",
3. check every license/exception has a rule -> "missing-rules",
4. check the dependency graph is acyclic    -> "dependency-cycle",
5. pick the OR-branch combination minimizing (conflicts, artifact
   obligations, component obligations, branch lexicographic order);
   small spaces are enumerated exhaustively, larger ones are searched
   with a complete branch-and-bound that only prunes combinations
   provably unable to improve the incumbent -- both paths compute the
   same optimum, so "conflict" is reported only when every combination
   is proven to conflict,
6. if every combination conflicts, report the minimal conflicting
   component set plus the confirmed subgraph -- never a release.

Statuses: ok | conflict | invalid-expression | missing-rules | dependency-cycle
"""

from itertools import product

from .common import DISTRIBUTIONS, LINKAGES, ClientError, stable_hash
from .expression import ExpressionError, alternatives, exception_ids, license_ids, parse

# Cap on the number of OR-branch combinations evaluated exhaustively.
MAX_COMBINATIONS = 4096


# ---------------------------------------------------------------------------
# request normalization
# ---------------------------------------------------------------------------

def normalize_request(body):
    """Validate and canonicalize an assessment request.

    Components and dependencies are sorted so that input ordering does not
    influence any downstream result or hash.
    """
    if not isinstance(body, dict):
        raise ClientError("request body must be a JSON object")

    distribution = body.get("distribution", "binary")
    if distribution not in DISTRIBUTIONS:
        raise ClientError("distribution must be one of %s" % (list(DISTRIBUTIONS),))

    product_name = body.get("product", "")
    if not isinstance(product_name, str):
        raise ClientError("product must be a string")

    raw_components = body.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise ClientError("components must be a non-empty array")

    seen = set()
    components = []
    for i, raw in enumerate(raw_components):
        if not isinstance(raw, dict):
            raise ClientError("components[%d] must be an object" % i)
        cid = raw.get("id")
        if not isinstance(cid, str) or not cid.strip():
            raise ClientError("components[%d].id must be a non-empty string" % i)
        cid = cid.strip()
        if cid in seen:
            raise ClientError("duplicate component id %r" % cid)
        seen.add(cid)
        license_text = raw.get("license")
        if not isinstance(license_text, str) or not license_text.strip():
            raise ClientError("component %r: license must be a non-empty string" % cid)
        linkage = raw.get("linkage", "static")
        if linkage not in LINKAGES:
            raise ClientError(
                "component %r: linkage must be one of %s" % (cid, list(LINKAGES))
            )
        name = raw.get("name", cid)
        version = raw.get("version", "")
        components.append(
            {
                "id": cid,
                "name": name if isinstance(name, str) else str(name),
                "version": version if isinstance(version, str) else str(version),
                "license": license_text.strip(),
                "linkage": linkage,
            }
        )
    components.sort(key=lambda c: c["id"])

    ids = {c["id"] for c in components}
    raw_deps = body.get("dependencies", [])
    if not isinstance(raw_deps, list):
        raise ClientError("dependencies must be an array")
    edges = set()
    for i, raw in enumerate(raw_deps):
        if not isinstance(raw, dict):
            raise ClientError("dependencies[%d] must be an object" % i)
        src, dst = raw.get("from"), raw.get("to")
        if not isinstance(src, str) or not isinstance(dst, str):
            raise ClientError("dependencies[%d] requires string 'from' and 'to'" % i)
        if src not in ids:
            raise ClientError(
                "dependencies[%d].from references unknown component %r" % (i, src)
            )
        if dst not in ids:
            raise ClientError(
                "dependencies[%d].to references unknown component %r" % (i, dst)
            )
        edges.add((src, dst))

    return {
        "product": product_name,
        "distribution": distribution,
        "components": components,
        "dependencies": [{"from": f, "to": t} for f, t in sorted(edges)],
    }


# ---------------------------------------------------------------------------
# graph helpers
# ---------------------------------------------------------------------------

def _find_cycles(nodes, edges):
    """Return the list of cycles as sorted strongly-connected components."""
    adj = {n: [] for n in nodes}
    self_loops = set()
    for f, t in edges:
        if f == t:
            self_loops.add(f)
        else:
            adj[f].append(t)
    for n in nodes:
        adj[n].sort()

    index_of, low, on_stack, stack, sccs = {}, {}, set(), [], []
    counter = [0]

    for start in nodes:
        if start in index_of:
            continue
        work = [(start, 0)]
        while work:
            node, child_idx = work[-1]
            if child_idx == 0:
                index_of[node] = low[node] = counter[0]
                counter[0] += 1
                stack.append(node)
                on_stack.add(node)
            descend = False
            neighbors = adj[node]
            i = child_idx
            while i < len(neighbors):
                nxt = neighbors[i]
                if nxt not in index_of:
                    work[-1] = (node, i + 1)
                    work.append((nxt, 0))
                    descend = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
                i += 1
            if descend:
                continue
            work.pop()
            if low[node] == index_of[node]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == node:
                        break
                if len(scc) > 1:
                    sccs.append(sorted(scc))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])

    for s in sorted(self_loops):
        sccs.append([s])
    sccs.sort()
    return sccs


def _deps_first_order(nodes, edges):
    """Topological order with dependencies before their dependents."""
    adj = {n: [] for n in nodes}
    for f, t in edges:
        if f != t:
            adj[f].append(t)
    for n in nodes:
        adj[n].sort()

    order = []
    visited = set()
    for start in nodes:
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(adj[start]))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, iter(adj[nxt])))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()
    return order


# ---------------------------------------------------------------------------
# evaluation of one branch selection
# ---------------------------------------------------------------------------

def _leaf_str(lid, exc):
    return "%s WITH %s" % (lid, exc) if exc else lid


def _gate(obligations, distribution, obligation_scopes):
    """Keep only obligations that apply in the given distribution scope."""
    return {
        o
        for o in obligations
        if distribution in obligation_scopes.get(o, DISTRIBUTIONS)
    }


def _evaluate(selection, components, comp_ids, edges, rules, distribution):
    """Propagate obligations and detect conflicts for one branch selection.

    ``selection`` maps component id -> tuple of (license_id, exception) leaves.
    """
    license_rules = rules["licenses"]
    exception_rules = rules.get("exceptions", {})
    obligation_scopes = rules.get("obligation_scopes", {})

    def obs_for(lid, exc):
        obs = set(license_rules[lid]["obligations"])
        if exc:
            er = exception_rules[exc]
            obs = (obs - set(er.get("removes", []))) | set(er.get("adds", []))
        return obs

    def propagates_for(lid, exc):
        if exc and exception_rules[exc].get("removes_propagation"):
            return set()
        return set(license_rules[lid]["propagates"])

    def exc_at(origin, lid):
        for l, e in selection[origin]:
            if l == lid:
                return e
        return ""

    # own obligations per component (always apply to the component itself)
    own_obs = {}
    for cid in comp_ids:
        obs = set()
        for lid, exc in selection[cid]:
            obs |= obs_for(lid, exc)
        own_obs[cid] = obs

    # Two kinds of upstream reachability, both keyed
    # license id -> set of origin components:
    #   presence: licenses co-existing in the same combined work (drives
    #             incompatibility conflicts); follows the linkages listed in
    #             the ruleset's combined_work_linkages.
    #   spread:   licenses whose obligations propagate onto this component
    #             (drives same-license clashes); follows each license's own
    #             "propagates" linkage list.
    combined_linkages = set(rules.get("combined_work_linkages", ["static", "dynamic"]))
    presence = {cid: {} for cid in comp_ids}
    spread = {cid: {} for cid in comp_ids}
    for cid in comp_ids:
        for lid, _ in selection[cid]:
            presence[cid].setdefault(lid, set()).add(cid)
            spread[cid].setdefault(lid, set()).add(cid)

    dependents = {cid: [] for cid in comp_ids}
    for f, t in edges:
        if f != t:
            dependents[t].append(f)
    for cid in comp_ids:
        dependents[cid].sort()

    # propagate upstream (dependencies processed before their dependents)
    for cid in _deps_first_order(comp_ids, edges):
        link = components[cid]["linkage"]
        for parent in dependents[cid]:
            if link in combined_linkages:
                for lid, origins in presence[cid].items():
                    presence[parent].setdefault(lid, set()).update(origins)
            for lid, origins in spread[cid].items():
                if any(link in propagates_for(lid, exc_at(o, lid)) for o in origins):
                    spread[parent].setdefault(lid, set()).update(origins)

    # conflict detection
    conflicts = []
    seen = set()
    for cid in comp_ids:
        # (a) licenses forced into the same combined work must be compatible
        present = sorted(presence[cid])
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                l1, l2 = present[i], present[j]
                if (
                    l2 in license_rules[l1]["incompatible_with"]
                    or l1 in license_rules[l2]["incompatible_with"]
                ):
                    involved = sorted({cid} | presence[cid][l1] | presence[cid][l2])
                    key = ("incompatible-licenses", tuple(involved), l1, l2)
                    if key not in seen:
                        seen.add(key)
                        conflicts.append(
                            {
                                "type": "incompatible-licenses",
                                "components": involved,
                                "licenses": [l1, l2],
                                "detail": "license %s is incompatible with %s at component %s"
                                % (l1, l2, cid),
                            }
                        )
        # (b) a component cannot be released under two different licenses
        same_license = [
            l
            for l in sorted(spread[cid])
            if any("same-license" in obs_for(l, exc_at(o, l)) for o in spread[cid][l])
        ]
        if len(same_license) > 1:
            involved = sorted({cid} | set().union(*(spread[cid][l] for l in same_license)))
            key = ("same-license-clash", tuple(involved), tuple(same_license))
            if key not in seen:
                seen.add(key)
                conflicts.append(
                    {
                        "type": "same-license-clash",
                        "components": involved,
                        "licenses": same_license,
                        "detail": "component %s would have to be released under "
                        "multiple licenses: %s" % (cid, ", ".join(same_license)),
                    }
                )
    conflicts.sort(key=lambda cf: (cf["type"], cf["components"], cf["licenses"]))

    artifact_obligations = set()
    for cid in comp_ids:
        artifact_obligations |= _gate(own_obs[cid], distribution, obligation_scopes)

    comp_report = []
    for cid in comp_ids:
        comp_report.append(
            {
                "id": cid,
                "licenses": [_leaf_str(l, e) for l, e in selection[cid]],
                "obligations": sorted(_gate(own_obs[cid], distribution, obligation_scopes)),
                "effective_licenses": sorted(presence[cid]),
                "propagated_from": {
                    l: sorted(spread[cid][l])
                    for l in sorted(spread[cid])
                    if spread[cid][l] != {cid}
                },
            }
        )

    return {
        "conflicts": conflicts,
        "artifact_obligations": sorted(artifact_obligations),
        "components": comp_report,
        "own_total": sum(len(own_obs[c]) for c in comp_ids),
    }


# ---------------------------------------------------------------------------
# branch-and-bound search (combination spaces beyond MAX_COMBINATIONS)
# ---------------------------------------------------------------------------

class _BranchBound:
    """Complete branch-and-bound search over OR-branch combinations.

    Finds exactly the selection the exhaustive path would find -- the one
    minimizing (conflict count, artifact obligation count, component
    obligation total, branch lexicographic order) -- without enumerating
    every combination.  A subtree is pruned only when every completion is
    provably no better than the incumbent, so the outcome never depends on
    the size of the combination space and ``conflict`` is reported only
    once every combination is proven to conflict.

    The conflict lower bound counts *guaranteed* conflicts: incompatible
    license pairs (resp. same-license obligations) that the licenses of the
    already-assigned components force into the same combined work (resp.
    onto the same component) no matter how the remaining components are
    chosen.  Guaranteed conflicts are a subset of the conflicts of every
    completion, so the bound never overestimates; complete selections are
    scored exactly with :func:`_evaluate`.

    When the optimum conflicts, :meth:`collect_summary` re-walks the search
    tree with the same lower bound to stream *every* least-conflict
    combination into a :class:`_LeastConflictSummary`.
    """

    def __init__(self, components, comp_ids, edges, rules, distribution, alts):
        self._components = components
        self._comp_ids = comp_ids
        self._edges = edges
        self._rules = rules
        self._distribution = distribution
        self._alts = alts

        license_rules = rules["licenses"]
        exception_rules = rules.get("exceptions", {})
        obligation_scopes = rules.get("obligation_scopes", {})
        combined_linkages = set(rules.get("combined_work_linkages", ["static", "dynamic"]))

        def obs_for(lid, exc):
            obs = set(license_rules[lid]["obligations"])
            if exc:
                er = exception_rules[exc]
                obs = (obs - set(er.get("removes", []))) | set(er.get("adds", []))
            return obs

        def propagates_for(lid, exc):
            if exc and exception_rules[exc].get("removes_propagation"):
                return frozenset()
            return frozenset(license_rules[lid]["propagates"])

        self._obs_for = obs_for
        self._propagates_for = propagates_for

        # symmetric incompatibility lookup (mirrors _evaluate's check)
        incompat = {}
        for lid, rule in license_rules.items():
            for other in rule["incompatible_with"]:
                incompat.setdefault(lid, set()).add(other)
                incompat.setdefault(other, set()).add(lid)
        self._incompat = incompat

        # Per-component alternative data.  Own obligations involve every
        # leaf; presence/spread use the first exception per license id
        # (mirroring _evaluate's exc_at).
        self._alt_obs = {}        # cid -> [frozenset of own obligations]
        self._alt_gated = {}      # cid -> [frozenset of in-scope obligations]
        self._alt_first_exc = {}  # cid -> [{license id: first exception}]
        self._min_own = {}        # cid -> min own-obligation count
        self._forced_gated = {}   # cid -> in-scope obligations every branch adds
        for cid in comp_ids:
            obs_list, gated_list, exc_list = [], [], []
            for alt in alts[cid]:
                first_exc = {}
                for lid, exc in alt:
                    if lid not in first_exc:
                        first_exc[lid] = exc
                obs = set()
                for lid, exc in alt:
                    obs |= obs_for(lid, exc)
                obs_list.append(frozenset(obs))
                gated_list.append(frozenset(_gate(obs, distribution, obligation_scopes)))
                exc_list.append(first_exc)
            self._alt_obs[cid] = obs_list
            self._alt_gated[cid] = gated_list
            self._alt_first_exc[cid] = exc_list
            self._min_own[cid] = min(len(o) for o in obs_list)
            forced = set(gated_list[0])
            for g in gated_list[1:]:
                forced &= g
            self._forced_gated[cid] = frozenset(forced)

        # Upward reachability: the parents a component's licenses flow to
        # (presence follows the combined-work linkages, spread follows each
        # license's own propagating linkages).
        dependents = {cid: [] for cid in comp_ids}
        for f, t in edges:
            if f != t:
                dependents[t].append(f)
        self._dependents = dependents
        self._link_of = {cid: components[cid]["linkage"] for cid in comp_ids}
        self._presence_targets = {
            cid: [cid] + self._reachable(cid, combined_linkages) for cid in comp_ids
        }
        self._spread_cache = {}

        # Incremental search state (maintained by _assign/_undo).
        self._assigned = {cid: -1 for cid in comp_ids}
        self._gp = {cid: {} for cid in comp_ids}  # guaranteed presence: lid -> origins
        self._slcount = {cid: {} for cid in comp_ids}  # obligated spread origins per lid
        self._slflag = {cid: set() for cid in comp_ids}  # lids with obligated origins
        self._lb = 0  # guaranteed-conflict lower bound
        self._cur_union = set()  # in-scope obligations of assigned components
        self._cur_na = 0
        self._cur_nt = 0
        self._trail = []

    def _reachable(self, start, linkages):
        """Ancestors that *start*'s licenses reach via the given linkages."""
        seen = {start}
        out = []
        stack = [start]
        while stack:
            node = stack.pop()
            if self._link_of[node] not in linkages:
                continue
            for parent in self._dependents[node]:
                if parent not in seen:
                    seen.add(parent)
                    out.append(parent)
                    stack.append(parent)
        return out

    def _spread_targets(self, cid, propagates):
        key = (cid, propagates)
        targets = self._spread_cache.get(key)
        if targets is None:
            targets = [cid] + self._reachable(cid, propagates)
            self._spread_cache[key] = targets
        return targets

    def _assign(self, cid, idx):
        """Assign component *cid* its alternative *idx*; return the undo mark."""
        trail = self._trail
        mark = len(trail)
        trail.append(("assign", cid))
        self._assigned[cid] = idx
        obs = self._alt_obs[cid][idx]
        added = self._alt_gated[cid][idx] - self._cur_union
        if added:
            self._cur_union |= added
            self._cur_na += len(added)
        self._cur_nt += len(obs)
        trail.append(("obs", added, len(obs)))
        first_exc = self._alt_first_exc[cid][idx]
        incompat = self._incompat
        # guaranteed presence -> guaranteed incompatible pairs
        for a in self._presence_targets[cid]:
            gp = self._gp[a]
            for lid in first_exc:
                origins = gp.get(lid)
                if origins is None:
                    gp[lid] = {cid}
                    forbidden = incompat.get(lid)
                    pairs = 0
                    if forbidden:
                        for other in gp:
                            if other != lid and other in forbidden:
                                pairs += 1
                    if pairs:
                        self._lb += pairs
                    trail.append(("gp-new", a, lid, pairs))
                else:
                    origins.add(cid)
                    trail.append(("gp-add", a, lid, cid))
        # guaranteed spread -> guaranteed same-license clashes
        obs_for = self._obs_for
        for lid, exc in first_exc.items():
            if "same-license" not in obs_for(lid, exc):
                continue
            for a in self._spread_targets(cid, self._propagates_for(lid, exc)):
                counts = self._slcount[a]
                cnt = counts.get(lid, 0)
                if cnt:
                    counts[lid] = cnt + 1
                    trail.append(("sl-add", a, lid))
                else:
                    counts[lid] = 1
                    flags = self._slflag[a]
                    flags.add(lid)
                    delta = 1 if len(flags) == 2 else 0
                    if delta:
                        self._lb += 1
                    trail.append(("sl-new", a, lid, delta))
        return mark

    def _undo(self, mark):
        """Undo every assignment effect recorded after *mark*."""
        trail = self._trail
        while len(trail) > mark:
            entry = trail.pop()
            kind = entry[0]
            if kind == "assign":
                self._assigned[entry[1]] = -1
            elif kind == "obs":
                _, added, obs_len = entry
                if added:
                    self._cur_union.difference_update(added)
                    self._cur_na -= len(added)
                self._cur_nt -= obs_len
            elif kind == "gp-new":
                _, a, lid, pairs = entry
                del self._gp[a][lid]
                if pairs:
                    self._lb -= pairs
            elif kind == "gp-add":
                _, a, lid, origin = entry
                self._gp[a][lid].discard(origin)
            elif kind == "sl-new":
                _, a, lid, delta = entry
                del self._slcount[a][lid]
                self._slflag[a].discard(lid)
                if delta:
                    self._lb -= delta
            else:  # "sl-add"
                _, a, lid = entry
                self._slcount[a][lid] -= 1

    def _min_completion_key(self):
        """Lexicographically smallest selection key among all completions."""
        alts = self._alts
        return tuple(
            alts[c][self._assigned[c]] if self._assigned[c] >= 0 else alts[c][0]
            for c in self._comp_ids
        )

    def _evaluate_current(self):
        sel = {c: self._alts[c][self._assigned[c]] for c in self._comp_ids}
        ev = _evaluate(
            sel, self._components, self._comp_ids, self._edges,
            self._rules, self._distribution,
        )
        sk = tuple(sel[c] for c in self._comp_ids)
        return sel, ev, sk

    def find_best(self):
        """Return (obj, selection, evaluation) for the optimal combination.

        ``obj`` is (conflict count, artifact obligation count, component
        obligation total); ties are broken by the lexicographic selection
        key -- the exact objective the exhaustive path minimizes.
        """
        comp_ids = self._comp_ids
        alts = self._alts
        order = _deps_first_order(comp_ids, self._edges)
        n = len(order)
        # suffix bounds over the assignment order (dependencies first, so
        # conflicts become guaranteed as early as possible)
        suffix_min_own = [0] * (n + 1)
        suffix_forced = [frozenset()] * (n + 1)
        for i in range(n - 1, -1, -1):
            cid = order[i]
            suffix_min_own[i] = suffix_min_own[i + 1] + self._min_own[cid]
            suffix_forced[i] = suffix_forced[i + 1] | self._forced_gated[cid]
        # cheapest branches first: the incumbent is found early (the search
        # order has no effect on the final result, only on its speed)
        value_order = {
            cid: sorted(
                range(len(alts[cid])),
                key=lambda i: (len(self._alt_obs[cid][i]), alts[cid][i]),
            )
            for cid in comp_ids
        }

        best = None  # [obj, selection key, selection, evaluation]
        # iterative DFS (component count is unbounded); each frame stores
        # [depth, alternative iterator, undo mark of the parent assignment]
        frames = [[0, iter(value_order[order[0]]), None]]
        while frames:
            depth, it, _ = frames[-1]
            try:
                idx = next(it)
            except StopIteration:
                mark = frames.pop()[2]
                if mark is not None:
                    self._undo(mark)
                continue
            cid = order[depth]
            mark = self._assign(cid, idx)
            lb = (
                self._lb,
                len(self._cur_union | suffix_forced[depth + 1]),
                self._cur_nt + suffix_min_own[depth + 1],
            )
            if best is not None and (
                lb > best[0]
                or (lb == best[0] and self._min_completion_key() >= best[1])
            ):
                self._undo(mark)
                continue
            if depth + 1 < n:
                frames.append([depth + 1, iter(value_order[order[depth + 1]]), mark])
                continue
            sel, ev, sk = self._evaluate_current()
            obj = (len(ev["conflicts"]), len(ev["artifact_obligations"]), ev["own_total"])
            if best is None or (obj, sk) < (best[0], best[1]):
                best = [obj, sk, sel, ev]
            self._undo(mark)
        return best[0], best[2], best[3]

    def collect_summary(self, min_conflicts, summary):
        """Stream every least-conflict combination into *summary*.

        Subtrees are pruned only when their guaranteed-conflict lower bound
        exceeds *min_conflicts* -- they cannot contain a least-conflict
        combination -- so every combination with exactly *min_conflicts*
        conflicts is visited exactly once, however many there are.  The
        combinations themselves are never stored; *summary* folds each one
        into the aggregate (see :class:`_LeastConflictSummary`).
        """
        comp_ids = self._comp_ids
        alts = self._alts
        for cid in comp_ids:
            if len(alts[cid]) == 1:
                self._assign(cid, 0)
        ors = [c for c in comp_ids if len(alts[c]) > 1]
        if not ors:
            sel, ev, _ = self._evaluate_current()
            if len(ev["conflicts"]) == min_conflicts:
                summary.add(sel, ev)
            return
        frames = [[0, iter(range(len(alts[ors[0]]))), None]]
        while frames:
            pos, it, _ = frames[-1]
            try:
                idx = next(it)
            except StopIteration:
                mark = frames.pop()[2]
                if mark is not None:
                    self._undo(mark)
                continue
            mark = self._assign(ors[pos], idx)
            if self._lb > min_conflicts:
                self._undo(mark)
                continue
            if pos + 1 < len(ors):
                nxt = ors[pos + 1]
                frames.append([pos + 1, iter(range(len(alts[nxt]))), mark])
                continue
            sel, ev, _ = self._evaluate_current()
            if len(ev["conflicts"]) == min_conflicts:
                summary.add(sel, ev)
            self._undo(mark)


# ---------------------------------------------------------------------------
# least-conflict aggregation (streaming, bounded memory)
# ---------------------------------------------------------------------------

class _LeastConflictSummary:
    """Streaming aggregation over every least-conflict combination.

    The number of least-conflict combinations is unbounded, so they are
    never stored.  Each visited combination folds into:

    - the union of its conflicts (deduplicated), and
    - per OR component, the set of branches it adopted at this level.

    Memory stays proportional to (distinct conflicts + OR branches), no
    matter how many least-conflict combinations exist, and the aggregated
    result depends only on the *set* of combinations -- not on the order
    they were visited in.
    """

    def __init__(self, or_comps):
        self._conflicts = {}  # (type, components, licenses) -> conflict
        self._branches = {cid: set() for cid in or_comps}  # cid -> branches seen

    def add(self, selection, evaluation):
        """Fold one least-conflict combination into the aggregate."""
        for cf in evaluation["conflicts"]:
            key = (cf["type"], tuple(cf["components"]), tuple(cf["licenses"]))
            self._conflicts.setdefault(key, cf)
        for cid, seen in self._branches.items():
            seen.add(selection[cid])

    def conflicts(self):
        """Sorted union of the conflicts of every visited combination."""
        return sorted(
            self._conflicts.values(),
            key=lambda cf: (cf["type"], cf["components"], cf["licenses"]),
        )

    def branch_varies(self, cid):
        """True when *cid* adopted more than one branch across combinations."""
        return len(self._branches[cid]) > 1


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def assess_normalized(norm, rules):
    """Run the full assessment on a normalized request; returns a result dict."""
    components = {c["id"]: c for c in norm["components"]}
    comp_ids = sorted(components)
    edges = [(d["from"], d["to"]) for d in norm["dependencies"]]
    distribution = norm["distribution"]

    # 1. parse expressions
    parsed, parse_errors = {}, []
    for cid in comp_ids:
        try:
            parsed[cid] = parse(components[cid]["license"])
        except ExpressionError as exc:
            parse_errors.append(
                {"component": cid, "message": exc.message, "position": exc.position}
            )
    if parse_errors:
        bad = sorted({e["component"] for e in parse_errors})
        return _finalize(
            {
                "status": "invalid-expression",
                "release": False,
                "distribution": distribution,
                "errors": parse_errors,
                "conflicting_components": bad,
                "confirmed_subgraph": {
                    "components": [
                        {"id": cid, "licenses": license_ids(parsed[cid]), "obligations": []}
                        for cid in comp_ids
                        if cid in parsed
                    ]
                },
            }
        )

    # 2. every referenced license / exception must have a rule
    license_rules = rules["licenses"]
    exception_rules = rules.get("exceptions", {})
    missing = {}
    for cid in comp_ids:
        miss = [l for l in license_ids(parsed[cid]) if l not in license_rules]
        miss += [e for e in exception_ids(parsed[cid]) if e not in exception_rules]
        if miss:
            missing[cid] = sorted(set(miss))
    if missing:
        return _finalize(
            {
                "status": "missing-rules",
                "release": False,
                "distribution": distribution,
                "missing": [{"component": cid, "ids": missing[cid]} for cid in sorted(missing)],
                "conflicting_components": sorted(missing),
                "confirmed_subgraph": {
                    "components": [
                        {"id": cid, "licenses": license_ids(parsed[cid]), "obligations": []}
                        for cid in comp_ids
                        if cid not in missing
                    ]
                },
            }
        )

    # 3. dependency graph must be acyclic
    cycles = _find_cycles(comp_ids, edges)
    if cycles:
        cycle_nodes = sorted({n for cyc in cycles for n in cyc})
        return _finalize(
            {
                "status": "dependency-cycle",
                "release": False,
                "distribution": distribution,
                "cycles": cycles,
                "conflicting_components": cycle_nodes,
                "confirmed_subgraph": {
                    "components": [
                        {"id": cid, "licenses": license_ids(parsed[cid]), "obligations": []}
                        for cid in comp_ids
                        if cid not in set(cycle_nodes)
                    ]
                },
            }
        )

    # 4. normalized OR alternatives per component
    alts = {}
    for cid in comp_ids:
        normalized = set()
        for alt in alternatives(parsed[cid]):
            normalized.add(tuple(sorted({(lid, exc or "") for lid, exc in alt})))
        alts[cid] = sorted(normalized)

    or_comps = [c for c in comp_ids if len(alts[c]) > 1]
    total = 1
    for c in or_comps:
        total *= len(alts[c])
        if total > MAX_COMBINATIONS:
            break

    # 5. pick the winning selection.  Both paths compute the same optimum:
    #    the selection minimizing (conflicts, artifact obligations, own
    #    obligations, selection lexicographically).
    #    - best: used when a conflict-free selection exists.
    #    - summary: aggregates every selection with the minimal conflict
    #      count (streamed, never stored) -> the union of their conflicts
    #      explains why no branch works, and the per-component branch sets
    #      decide which OR components are confirmed.
    if total <= MAX_COMBINATIONS:
        mode = "exhaustive"
        best = None  # (obj, sel_key, selection, evaluation)
        min_conflicts = None
        summary = None
        for combo in product(*[alts[c] for c in or_comps]):
            sel = {c: alts[c][0] for c in comp_ids}
            sel.update(dict(zip(or_comps, combo)))
            ev = _evaluate(sel, components, comp_ids, edges, rules, distribution)
            sk = tuple(sel[c] for c in comp_ids)
            obj = (len(ev["conflicts"]), len(ev["artifact_obligations"]), ev["own_total"])
            if best is None or (obj, sk) < (best[0], best[1]):
                best = (obj, sk, sel, ev)
            nc = obj[0]
            if min_conflicts is None or nc < min_conflicts:
                min_conflicts = nc
                summary = _LeastConflictSummary(or_comps)
            if nc == min_conflicts:
                summary.add(sel, ev)
        _, _, _, best_ev = best
    else:
        # large search spaces: complete branch-and-bound; pruning only skips
        # combinations provably unable to improve the incumbent, so the
        # result is identical to exhaustive enumeration and "conflict" is
        # reported only once every combination is proven to conflict
        mode = "branch-and-bound"
        searcher = _BranchBound(components, comp_ids, edges, rules, distribution, alts)
        best_obj, _, best_ev = searcher.find_best()
        min_conflicts = best_obj[0]
        summary = None
        if min_conflicts > 0:
            summary = _LeastConflictSummary(or_comps)
            searcher.collect_summary(min_conflicts, summary)

    # 6a. every branch conflicts -> no release conclusion
    if min_conflicts > 0:
        # minimal conflicting component set: union of the components involved
        # in the conflicts of every least-conflict combination -- aggregated
        # over all of them, so the explanation stays complete however many
        # least-conflict combinations exist
        conflicts = summary.conflicts()
        conflict_components = sorted({c for cf in conflicts for c in cf["components"]})
        cset = set(conflict_components)
        comp_map = {c["id"]: c for c in best_ev["components"]}
        confirmed = []
        for cid in comp_ids:
            if cid in cset:
                continue
            if len(alts[cid]) > 1:
                # an OR component is only confirmed when every least-conflict
                # combination adopts the same branch for it
                if summary.branch_varies(cid):
                    continue
            confirmed.append(
                {
                    "id": cid,
                    "licenses": comp_map[cid]["licenses"],
                    "obligations": comp_map[cid]["obligations"],
                }
            )
        return _finalize(
            {
                "status": "conflict",
                "release": False,
                "distribution": distribution,
                "selection_mode": mode,
                "conflicts": conflicts,
                "conflicting_components": conflict_components,
                "confirmed_subgraph": {"components": confirmed},
            }
        )

    # 6b. releasable
    return _finalize(
        {
            "status": "ok",
            "release": True,
            "distribution": distribution,
            "selection_mode": mode,
            "artifact_obligations": best_ev["artifact_obligations"],
            "adopted": {
                c["id"]: " AND ".join(c["licenses"]) for c in best_ev["components"]
            },
            "components": best_ev["components"],
        }
    )


def assess(body, rules):
    """Convenience wrapper: normalize + assess a raw request body."""
    return assess_normalized(normalize_request(body), rules)


def _finalize(result):
    result["summary_hash"] = stable_hash(result)
    return result


# ---------------------------------------------------------------------------
# confirmation (release record)
# ---------------------------------------------------------------------------

def build_confirmation(request, result, rules, assessment_id, ruleset=None):
    """Build the confirmed release record: manifest, adopted branches,
    NOTICE text and a replay request that reproduces the assessment.

    ``rules`` is the exact normalized rules snapshot the assessment ran
    against.  The replay request embeds that snapshot inline (rather than
    naming a mutable ruleset), so it stays submittable and bit-for-bit
    reproducible even after the same-named ruleset is overwritten -- or
    when the original assessment used inline rules.  ``ruleset`` is kept
    only as human-readable provenance (None for inline-rule assessments).
    """
    info = {c["id"]: c for c in request["components"]}
    artifacts = []
    adopted = {}
    notice_blocks = []
    for crep in result["components"]:
        cid = crep["id"]
        meta = info[cid]
        expr = " AND ".join(crep["licenses"])
        adopted[cid] = expr
        artifacts.append(
            {
                "id": cid,
                "name": meta["name"],
                "version": meta["version"],
                "license": expr,
                "obligations": crep["obligations"],
            }
        )
        if "attribution" in crep["obligations"] or "license-text" in crep["obligations"]:
            notice_blocks.append((meta["name"], meta["version"], cid, expr))

    product = request.get("product") or "This artifact"
    lines = ["%s includes the following third-party components:" % product, ""]
    for name, version, cid, expr in notice_blocks:
        heading = "* %s %s (%s)" % (name, version, cid)
        lines.append(heading.rstrip())
        lines.append("  License: %s" % expr)
        lines.append("")
    if not notice_blocks:
        lines.append("(no attribution obligations)")
    notice = "\n".join(lines).rstrip() + "\n"

    # The replay request is the complete POST /v1/assessments body: the
    # normalized request with the exact normalized rules snapshot inlined.
    # Re-submitting it is independent of any current same-named ruleset and
    # lands on the same assessment id and summary_hash.
    replay_request = dict(request)
    replay_request["rules"] = rules

    body = {
        "status": "confirmed",
        "assessment_id": assessment_id,
        "product": request.get("product", ""),
        "distribution": result["distribution"],
        "manifest": {"artifacts": artifacts},
        "adopted_licenses": adopted,
        "notice": notice,
        "replay": {
            "endpoint": "POST /v1/assessments",
            "ruleset": ruleset,
            "rules_hash": stable_hash(rules),
            "request": replay_request,
            "expected_summary_hash": result["summary_hash"],
        },
    }
    body["confirmation_hash"] = stable_hash(body)
    return body
