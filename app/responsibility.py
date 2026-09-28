"""Responsibility planning: staff every obligation item of a released
(``status == "ok"``) assessment with a qualified executor and reviewer.

Pipeline for one responsibility-plan request:

1. normalize/validate the submitted people -- id, execute/review
   qualifications, total capacity and the per-role costs.  People are
   sorted by id and every qualification list is deduplicated and sorted,
   so the input ordering of the ``people`` array and of the
   qualification lists can never change the plan or the ``plan_hash``;
2. expand the assessment result into responsibility items -- one item
   per *artifact* obligation (``artifact:<obligation>``) and one item
   per *component* obligation (``component:<component>:<obligation>``),
   sorted by item id;
3. build, per item, the candidate staffing pairs ``(executor,
   reviewer)``: the executor must hold the item's obligation under
   ``qualifications.execute``, the reviewer under
   ``qualifications.review``, and the two must be different people.
   Every assigned role consumes one unit of that person's total
   capacity, so a person may hold any mix of execute/review roles as
   long as the total stays within ``capacity``;
4. solve a *global* optimization -- never executor-first-then-reviewer
   greedy, never a fixed cut-off -- minimizing the lexicographic
   objective::

       (- staffed items, total cost, (item, executor, reviewer) ...)

   i.e. first maximize the number of items staffed with **both** roles,
   then minimize the total cost (for every staffed item, the executor's
   execute cost plus the reviewer's review cost), then break remaining
   ties by the sequence of ``(item id, executor id, reviewer id)``
   triples in item-id order.  Small choice spaces are enumerated
   exhaustively; larger spaces use a complete branch-and-bound whose
   per-node bounds come from an exact min-cost-flow relaxation of the
   remaining subproblem, so only provably non-improving subtrees are
   pruned and both paths return the identical optimum
   (``selection_mode`` reports ``exhaustive`` / ``branch-and-bound``);
5. when not every item can be staffed, the same objective still yields
   the best partial plan, returned with HTTP 409 together with the
   unstaffed items and the reason each is blocked:
   ``no-qualified-executor`` / ``no-qualified-reviewer`` /
   ``no-distinct-pair`` (qualification limits) or
   ``insufficient-capacity`` (capacity limits).

Costs are exact rational numbers internally (fractional inputs are
scaled to integers for the search), so floating-point rounding can never
disturb the optimization or the tie-break; responses report costs as
JSON numbers (integers when integral).
"""

import heapq
import math
from fractions import Fraction

from .common import ClientError, ConflictError, stable_hash

# Choice-space cap for exhaustive enumeration: the product over items of
# (candidate staffing pairs + 1).  Larger spaces use branch-and-bound.
MAX_PLAN_COMBINATIONS = 65536

_PERSON_FIELDS = {"id", "qualifications", "capacity", "costs"}
_ROLE_FIELDS = {"execute", "review"}


class PartialPlanError(ConflictError):
    """HTTP 409 carrying the structured best-partial-plan payload."""

    def __init__(self, payload):
        super().__init__(payload["error"])
        self.payload = payload


# ---------------------------------------------------------------------------
# request normalization
# ---------------------------------------------------------------------------

def _qualification_list(value, where):
    if not isinstance(value, list):
        raise ClientError("%s must be an array of obligation ids" % where)
    out = set()
    for i, ob in enumerate(value):
        if not isinstance(ob, str) or not ob.strip():
            raise ClientError(
                "%s[%d] must be a non-empty obligation id string" % (where, i)
            )
        out.add(ob.strip())
    return sorted(out)


def _cost(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClientError("%s must be a non-negative number" % where)
    if not math.isfinite(value) or value < 0:
        raise ClientError("%s must be a non-negative number" % where)
    # Fraction(int) and Fraction(float) are both exact, so later sums and
    # comparisons never see floating-point rounding.
    return Fraction(value)


def normalize_people(body):
    """Validate and canonicalize the submitted people.

    Returns the person list sorted by id with deduplicated, sorted
    qualification lists and exact rational costs, so the input ordering
    of people and qualifications cannot influence the result.
    """
    if not isinstance(body, dict):
        raise ClientError("request body must be a JSON object")
    raw_people = body.get("people")
    if not isinstance(raw_people, list):
        raise ClientError("people must be an array of person objects")

    people = []
    seen = set()
    for i, raw in enumerate(raw_people):
        where = "people[%d]" % i
        if not isinstance(raw, dict):
            raise ClientError("%s must be an object" % where)
        unknown = sorted(set(raw) - _PERSON_FIELDS)
        if unknown:
            raise ClientError("%s: unknown fields %s" % (where, unknown))
        pid = raw.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise ClientError("%s.id must be a non-empty string" % where)
        pid = pid.strip()
        if pid in seen:
            raise ClientError("duplicate person id %r" % pid)
        seen.add(pid)

        quals = raw.get("qualifications", {})
        if not isinstance(quals, dict):
            raise ClientError("%s.qualifications must be an object" % where)
        unknown = sorted(set(quals) - _ROLE_FIELDS)
        if unknown:
            raise ClientError(
                "%s.qualifications: unknown fields %s" % (where, unknown)
            )
        execute = _qualification_list(
            quals.get("execute", []), "%s.qualifications.execute" % where
        )
        review = _qualification_list(
            quals.get("review", []), "%s.qualifications.review" % where
        )

        capacity = raw.get("capacity")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise ClientError("%s.capacity must be a non-negative integer" % where)

        costs = raw.get("costs", {})
        if not isinstance(costs, dict):
            raise ClientError("%s.costs must be an object" % where)
        unknown = sorted(set(costs) - _ROLE_FIELDS)
        if unknown:
            raise ClientError("%s.costs: unknown fields %s" % (where, unknown))
        execute_cost = _cost(costs.get("execute", 0), "%s.costs.execute" % where)
        review_cost = _cost(costs.get("review", 0), "%s.costs.review" % where)

        people.append(
            {
                "id": pid,
                "execute": execute,
                "review": review,
                "capacity": capacity,
                "execute_cost": execute_cost,
                "review_cost": review_cost,
            }
        )
    people.sort(key=lambda p: p["id"])
    return people


# ---------------------------------------------------------------------------
# item expansion and candidate staffing pairs
# ---------------------------------------------------------------------------

def expand_items(result):
    """Expand an ``ok`` assessment result into responsibility items.

    One item per artifact obligation and one per component obligation,
    sorted by item id.
    """
    items = []
    for ob in sorted(result.get("artifact_obligations", [])):
        items.append({"id": "artifact:%s" % ob, "kind": "artifact", "obligation": ob})
    for crep in result.get("components", []):
        cid = crep["id"]
        for ob in sorted(crep.get("obligations", [])):
            items.append(
                {
                    "id": "component:%s:%s" % (cid, ob),
                    "kind": "component",
                    "component": cid,
                    "obligation": ob,
                }
            )
    items.sort(key=lambda it: it["id"])
    return items


def build_candidates(items, people):
    """Per item, the feasible staffing pairs and qualified people.

    Returns ``(candidates, qualified)`` where ``candidates[j]`` is the
    tuple of ``(executor_index, reviewer_index, pair_cost)`` pairs for
    item *j*, sorted by ``(executor, reviewer)`` (people are id-sorted,
    so index order is id order), and ``qualified[j]`` is the pair of
    qualified executor/reviewer index lists.  A pair requires distinct
    people; capacity is enforced by the search, not here.
    """
    exec_sets = [set(p["execute"]) for p in people]
    rev_sets = [set(p["review"]) for p in people]
    candidates = []
    qualified = []
    for it in items:
        ob = it["obligation"]
        execs = [j for j in range(len(people)) if ob in exec_sets[j]]
        revs = [j for j in range(len(people)) if ob in rev_sets[j]]
        pairs = []
        for e in execs:
            ecost = people[e]["execute_cost"]
            for r in revs:
                if e == r:
                    continue
                pairs.append((e, r, ecost + people[r]["review_cost"]))
        pairs.sort(key=lambda t: (t[0], t[1]))
        candidates.append(tuple(pairs))
        qualified.append((execs, revs))
    return candidates, qualified


# ---------------------------------------------------------------------------
# min-cost flow (exact value) -- the relaxation behind the bounds
# ---------------------------------------------------------------------------

def _min_cost_exact_flow(graph, source, sink, value):
    """Min cost to push exactly *value* units from *source* to *sink*.

    *graph* is a list of adjacency lists of ``[to, capacity, cost,
    reverse_index]`` edges with non-negative costs.  Successive shortest
    augmenting paths (Dijkstra with Johnson potentials) are pushed until
    *value* units flow; returns the total cost, or ``None`` when the
    sink cannot receive *value* units.  The graph is mutated.
    """
    if value <= 0:
        return 0
    n = len(graph)
    potential = [0] * n
    total = 0
    flow = 0
    while flow < value:
        dist = [None] * n
        prev = [None] * n
        dist[source] = 0
        heap = [(0, source)]
        while heap:
            d, u = heapq.heappop(heap)
            if d != dist[u]:
                continue
            for ei, e in enumerate(graph[u]):
                if e[1] <= 0:
                    continue
                v = e[0]
                nd = d + e[2] + potential[u] - potential[v]
                if dist[v] is None or nd < dist[v]:
                    dist[v] = nd
                    prev[v] = (u, ei)
                    heapq.heappush(heap, (nd, v))
        if dist[sink] is None:
            return None
        for u in range(n):
            if dist[u] is not None:
                potential[u] += dist[u]
        # potential[sink] is now the real shortest-path cost
        f = value - flow
        v = sink
        while v != source:
            u, ei = prev[v]
            if graph[u][ei][1] < f:
                f = graph[u][ei][1]
            v = u
        v = sink
        while v != source:
            u, ei = prev[v]
            e = graph[u][ei]
            e[1] -= f
            graph[v][e[3]][1] += f
            v = u
        total += f * potential[sink]
        flow += f
    return total


def _completion_bound(i, candidates, exec_qual, rev_qual, exec_costs, rev_costs,
                      capacities, rem, k):
    """Admissible cost lower bound for staffing *k* more of items[i:].

    Builds the flow relaxation of the remaining subproblem -- source to
    people (remaining capacity), people to per-item role nodes (unit
    capacity, role cost, qualification edges), role nodes to sink -- and
    returns the min cost of pushing exactly ``2k`` units (one per role of
    the *k* items still to staff).  The relaxation ignores the
    distinct-people-per-item constraint and allows partially staffed
    items, so its optimum never exceeds the true cost of *k* more
    completions; ``None`` means even the relaxation cannot staff *k*
    more items, i.e. the count target is unreachable from this node.
    """
    n_people = len(capacities)
    n_items = len(candidates) - i
    # nodes: 0 = source, 1 = sink, 2..2+p-1 people, then two role nodes
    # per remaining item
    graph = [[] for _ in range(2 + n_people + 2 * n_items)]

    def add(u, v, cap, cost):
        graph[u].append([v, cap, cost, len(graph[v])])
        graph[v].append([u, 0, -cost, len(graph[u]) - 1])

    for pi in range(n_people):
        if rem[pi] > 0:
            add(0, 2 + pi, rem[pi], 0)
    base = 2 + n_people
    for t in range(n_items):
        j = i + t
        enode = base + 2 * t
        rnode = enode + 1
        add(enode, 1, 1, 0)
        add(rnode, 1, 1, 0)
        for pi in exec_qual[j]:
            if rem[pi] > 0:
                add(2 + pi, enode, 1, exec_costs[pi])
        for pi in rev_qual[j]:
            if rem[pi] > 0:
                add(2 + pi, rnode, 1, rev_costs[pi])
    return _min_cost_exact_flow(graph, 0, 1, 2 * k)


# ---------------------------------------------------------------------------
# global optimization: exact lexicographic objective over staffing choices
# ---------------------------------------------------------------------------

def solve_exhaustive(item_ids, candidates, capacities, person_ids, role_costs):
    """Reference oracle: visit every capacity-feasible combination of
    per-item staffing choices (pair or skip) without any pruning.
    Returns ``(best_key, best_assignment)`` where *best_key* is
    ``(-staffed_count, total_cost, sequence)`` and *best_assignment*
    maps each item index to ``(executor, reviewer)`` or ``None``.
    """
    return _search(item_ids, candidates, capacities, person_ids, role_costs,
                   prune=False)


def solve_branch_bound(item_ids, candidates, capacities, person_ids, role_costs):
    """Complete branch-and-bound twin of :func:`solve_exhaustive`.

    Explores items in id order, trying staffing pairs (in executor /
    reviewer id order) before the skip choice, seeded by a greedy
    incumbent.  A subtree is pruned only when it is provably unable to
    improve the incumbent:

    * the number of still-feasible remaining items (and the remaining
      capacity pool) caps the reachable staffed count -- prune when it
      cannot reach the incumbent's count;
    * when the count can at best be tied, every completion must staff
      exactly the missing number of items, so the sum of the cheapest
      per-item pair costs is an admissible cost lower bound -- prune
      when it exceeds the incumbent's cost, or ties it while the partial
      ``(item, executor, reviewer)`` sequence (a prefix of every
      completion) already exceeds the incumbent's;
    * the same count/cost questions answered *exactly* for the relaxed
      subproblem by a min-cost-flow computation (partial staffing
      allowed, distinctness ignored): prune when the relaxation cannot
      reach the count target, or its min cost already exceeds -- or
      exactly ties with a worse sequence -- the incumbent.

    Both paths therefore return the identical optimum.
    """
    return _search(item_ids, candidates, capacities, person_ids, role_costs,
                   prune=True)


def _search(item_ids, candidates, capacities, person_ids, role_costs, prune):
    n = len(item_ids)
    exec_costs, rev_costs = role_costs
    min_costs = [
        min((c for _e, _r, c in cand), default=0) for cand in candidates
    ]
    # qualification lists derived from the valid pairs: every real
    # completion uses a valid pair, so restricting the relaxation to
    # pair members keeps it admissible and tighter
    exec_qual = [tuple(sorted({e for e, _r, _c in cand})) for cand in candidates]
    rev_qual = [tuple(sorted({r for _e, r, _c in cand})) for cand in candidates]

    best_key = [None]
    best_assign = [None]

    def consider(assign, count, cost, seq):
        key = (-count, cost, seq)
        if best_key[0] is None or key < best_key[0]:
            best_key[0] = key
            best_assign[0] = list(assign)

    assign = [None] * n

    if prune:
        # greedy incumbent: each item in id order takes the cheapest
        # capacity-feasible pair, ties broken by (executor, reviewer).
        # Never changes the optimum, only pruning strength.
        rem = list(capacities)
        g_count = 0
        g_cost = 0
        g_seq = []
        for j in range(n):
            pick = None
            for (e, r, c) in candidates[j]:
                if rem[e] >= 1 and rem[r] >= 1 and (pick is None or (c, e, r) < pick):
                    pick = (c, e, r)
            if pick is not None:
                c, e, r = pick
                rem[e] -= 1
                rem[r] -= 1
                assign[j] = (e, r)
                g_count += 1
                g_cost += c
                g_seq.append((item_ids[j], person_ids[e], person_ids[r]))
        consider(assign, g_count, g_cost, tuple(g_seq))
        assign = [None] * n

    if n == 0:
        consider(assign, 0, 0, ())
        return best_key[0], best_assign[0]

    # iterative depth-first search; frames are [item index, next choice
    # pointer, applied choice] where a choice is ("pair", e, r, cost) or
    # ("skip",).  Items are decided in id order, so the partial sequence
    # is always a prefix of any completion's sequence.
    rem = list(capacities)
    rem_sum = sum(capacities)
    seq = []
    count = 0
    cost = 0
    stack = [[0, 0, None]]
    while stack:
        frame = stack[-1]
        i = frame[0]
        applied = frame[2]
        if applied is not None:
            # returning from a child: undo the choice that led to it
            frame[2] = None
            if applied[0] == "pair":
                _k, e, r, c = applied
                rem[e] += 1
                rem[r] += 1
                rem_sum += 2
                count -= 1
                cost -= c
                seq.pop()
                assign[i] = None
        if i == n:
            consider(assign, count, cost, tuple(seq))
            stack.pop()
            continue
        if prune and frame[1] == 0 and best_key[0] is not None:
            neg_count, best_cost, best_seq = best_key[0]
            best_count = -neg_count
            feasible = 0
            feasible_mins = []
            for j in range(i, n):
                for (e, r, _c) in candidates[j]:
                    if rem[e] >= 1 and rem[r] >= 1:
                        feasible += 1
                        feasible_mins.append(min_costs[j])
                        break
            upper = count + min(feasible, rem_sum // 2)
            if upper < best_count:
                stack.pop()
                continue
            if upper == best_count:
                need = best_count - count
                # every completion staffs exactly `need` of the feasible
                # remaining items; the cheapest static pair costs give an
                # admissible lower bound on the additional cost
                feasible_mins.sort()
                lower = cost + sum(feasible_mins[:need])
                if lower > best_cost or (
                    lower == best_cost and tuple(seq) > best_seq
                ):
                    stack.pop()
                    continue
                if need > 0:
                    bound = _completion_bound(
                        i, candidates, exec_qual, rev_qual,
                        exec_costs, rev_costs, capacities, rem, need,
                    )
                    if bound is None:
                        stack.pop()
                        continue
                    lower = cost + bound
                    if lower > best_cost or (
                        lower == best_cost and tuple(seq) > best_seq
                    ):
                        stack.pop()
                        continue
        descended = False
        cand = candidates[i]
        total_choices = len(cand) + 1
        while frame[1] < total_choices:
            choice = frame[1]
            frame[1] += 1
            if choice == len(cand):
                frame[2] = ("skip",)
                stack.append([i + 1, 0, None])
                descended = True
                break
            e, r, c = cand[choice]
            if rem[e] >= 1 and rem[r] >= 1:
                rem[e] -= 1
                rem[r] -= 1
                rem_sum -= 2
                count += 1
                cost += c
                seq.append((item_ids[i], person_ids[e], person_ids[r]))
                assign[i] = (e, r)
                frame[2] = ("pair", e, r, c)
                stack.append([i + 1, 0, None])
                descended = True
                break
        if not descended:
            stack.pop()
    return best_key[0], best_assign[0]


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def _num(frac):
    """Render an exact rational cost as a JSON number (int when integral)."""
    if frac.denominator == 1:
        return frac.numerator
    return float(frac)


def build_plan(body, result, assessment_id):
    """Build the responsibility plan for an ``ok`` assessment.

    The caller guarantees ``result["status"] == "ok"``; the gate lives in
    the API layer so a non-release assessment never reaches this code.
    When not every item can be staffed, the best partial plan is raised
    as :class:`PartialPlanError` (HTTP 409) with the same payload shape.
    """
    people = normalize_people(body)
    items = expand_items(result)
    candidates, qualified = build_candidates(items, people)

    item_ids = [it["id"] for it in items]
    person_ids = [p["id"] for p in people]
    capacities = [p["capacity"] for p in people]

    # scale all costs to integers (lcm of denominators) so the search and
    # the flow relaxation run on exact, fast integer arithmetic
    scale = 1
    for p in people:
        for c in (p["execute_cost"], p["review_cost"]):
            scale = scale * c.denominator // math.gcd(scale, c.denominator)

    def scaled(frac):
        return frac.numerator * (scale // frac.denominator)

    int_candidates = [
        tuple((e, r, scaled(c)) for e, r, c in cand) for cand in candidates
    ]
    role_costs = (
        [scaled(p["execute_cost"]) for p in people],
        [scaled(p["review_cost"]) for p in people],
    )

    combinations = 1
    for cand in int_candidates:
        combinations *= len(cand) + 1
        if combinations > MAX_PLAN_COMBINATIONS:
            break
    if combinations <= MAX_PLAN_COMBINATIONS:
        mode = "exhaustive"
        _key, assign = solve_exhaustive(
            item_ids, int_candidates, capacities, person_ids, role_costs
        )
    else:
        mode = "branch-and-bound"
        _key, assign = solve_branch_bound(
            item_ids, int_candidates, capacities, person_ids, role_costs
        )

    assignments = []
    unassigned = []
    exec_used = [0] * len(people)
    rev_used = [0] * len(people)
    total = Fraction(0)
    for j, it in enumerate(items):
        pair = assign[j]
        if pair is not None:
            e, r = pair
            pcost = people[e]["execute_cost"] + people[r]["review_cost"]
            total += pcost
            exec_used[e] += 1
            rev_used[r] += 1
            entry = {
                "item": it["id"],
                "kind": it["kind"],
                "obligation": it["obligation"],
                "executor": person_ids[e],
                "reviewer": person_ids[r],
                "cost": _num(pcost),
            }
            if it["kind"] == "component":
                entry["component"] = it["component"]
            assignments.append(entry)
            continue
        execs, revs = qualified[j]
        qe = [person_ids[k] for k in execs]
        qr = [person_ids[k] for k in revs]
        if not qe:
            reason = "no-qualified-executor"
            detail = (
                "no submitted person is qualified to execute obligation %r"
                % it["obligation"]
            )
        elif not qr:
            reason = "no-qualified-reviewer"
            detail = (
                "no submitted person is qualified to review obligation %r"
                % it["obligation"]
            )
        elif len(qe) == 1 and qe == qr:
            reason = "no-distinct-pair"
            detail = (
                "only %r is qualified to execute and to review obligation %r; "
                "executor and reviewer must be different people"
                % (qe[0], it["obligation"])
            )
        else:
            reason = "insufficient-capacity"
            detail = (
                "qualified people exist for obligation %r, but their "
                "remaining capacity is exhausted by the other assignments "
                "in the optimal plan" % it["obligation"]
            )
        entry = {
            "item": it["id"],
            "kind": it["kind"],
            "obligation": it["obligation"],
            "reason": reason,
            "detail": detail,
            "qualified_executors": qe,
            "qualified_reviewers": qr,
        }
        if it["kind"] == "component":
            entry["component"] = it["component"]
        unassigned.append(entry)

    usage = [
        {
            "person": p["id"],
            "execute": exec_used[k],
            "review": rev_used[k],
            "used": exec_used[k] + rev_used[k],
            "capacity": p["capacity"],
        }
        for k, p in enumerate(people)
    ]
    assigned_count = len(assignments)
    ready = assigned_count == len(items)
    out = {
        "assessment_id": assessment_id,
        "ready": ready,
        "selection_mode": mode,
        "items_total": len(items),
        "items_assigned": assigned_count,
        "assignments": assignments,
        "unassigned": unassigned,
        "total_cost": _num(total),
        "capacity_usage": usage,
    }
    out["plan_hash"] = stable_hash(out)
    if not ready:
        payload = dict(out)
        payload["error"] = (
            "cannot staff every responsibility item: %d of %d items assigned"
            % (assigned_count, len(items))
        )
        raise PartialPlanError(payload)
    return out
