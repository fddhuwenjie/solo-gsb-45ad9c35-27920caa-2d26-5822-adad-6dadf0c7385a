"""Assessment comparison: atomic diffs and counterfactual attribution.

Given two persisted assessments -- a baseline and a target -- each
carrying its normalized request and the rules snapshot it ran with:

1. the state change is decomposed into atomic, stably numbered diffs:
   the distribution scope, the product label, single component records,
   single dependency edges, single license rules, single exception
   rules, single obligation-scope entries and single combined-work
   linkages (the last three keep the model complete: rolling back
   *every* diff reproduces the persisted baseline state exactly);
2. starting from the target state, subsets of the diffs are
   counterfactually rolled back (reverted to their baseline side) and
   every legal intermediate state is re-assessed;
3. the answer is the *globally* minimal rollback set restoring the
   baseline conclusion -- status, adopted branches, artifact and
   component obligations and the conflict explanation.  Sets are
   compared by size first, then by the lexicographic order of their
   diff-id sequence, which makes the answer unique.  The search is
   complete (increasing size, lexicographic combination order): never
   greedy, never truncated at a fixed size, and illegal intermediate
   states (e.g. an edge left pointing at a reverted component) are
   skipped, never counted as hits.

Because the full rollback always reproduces the baseline state, a
restoring set always exists and the search always terminates with a
hit.
"""

import copy
from itertools import combinations

from .common import ClientError, stable_hash
from .rules import validate_ruleset
from .solver import assess_normalized, normalize_request

# Diff kinds in stable numbering order.
_KIND_ORDER = (
    "distribution",
    "product",
    "component",
    "dependency",
    "license-rule",
    "exception-rule",
    "obligation-scope",
    "combined-work-linkage",
)

# Result fields the counterfactual conclusion is compared on: the
# status, the adopted branches, the artifact and per-component
# obligations and the conflict explanation (for every non-ok status).
# Incidental fields (hashes, selection mode, distribution) take no part.
_SIGNATURE_KEYS = (
    "adopted",
    "artifact_obligations",
    "conflicts",
    "conflicting_components",
    "confirmed_subgraph",
    "errors",
    "missing",
    "cycles",
)


# ---------------------------------------------------------------------------
# atomic diffs
# ---------------------------------------------------------------------------

def _change_of(before, after):
    if before is None:
        return "added"
    if after is None:
        return "removed"
    return "changed"


def _record_diffs(diffs, kind, field, before_map, after_map):
    """One diff per key whose record differs between the two maps."""
    for key in sorted(set(before_map) | set(after_map)):
        before, after = before_map.get(key), after_map.get(key)
        if before != after:
            diffs.append(
                {
                    "kind": kind,
                    field: key,
                    "change": _change_of(before, after),
                    "before": before,
                    "after": after,
                }
            )


def _membership_diffs(diffs, kind, field, before_set, after_set):
    """One diff per member present on exactly one side (edge/linkage)."""
    for member in sorted(before_set | after_set):
        before, after = member in before_set, member in after_set
        if before == after:
            continue
        diff = {
            "kind": kind,
            "change": "removed" if before else "added",
            "before": before,
            "after": after,
        }
        if kind == "dependency":
            diff["from"], diff["to"] = member
        else:
            diff[field] = member
        diffs.append(diff)


def _diff_key(diff):
    kind = diff["kind"]
    if kind == "component":
        return (diff["component"],)
    if kind == "dependency":
        return (diff["from"], diff["to"])
    if kind == "license-rule":
        return (diff["license"],)
    if kind == "exception-rule":
        return (diff["exception"],)
    if kind == "obligation-scope":
        return (diff["obligation"],)
    if kind == "combined-work-linkage":
        return (diff["linkage"],)
    return ()


def build_diffs(b_request, b_rules, t_request, t_rules):
    """Decompose the baseline->target state change into atomic diffs.

    Every diff carries ``before`` (baseline-side value) and ``after``
    (target-side value); for record/rule diffs ``None`` marks the side
    where the entry does not exist, for membership diffs (dependency
    edges, combined-work linkages) a boolean marks presence.  Diffs are
    sorted deterministically and numbered ``D0001``... in that order,
    so the same assessment pair always yields the same numbering.
    """
    diffs = []
    for kind in ("distribution", "product"):
        before = b_request.get(kind, "" if kind == "product" else None)
        after = t_request.get(kind, "" if kind == "product" else None)
        if before != after:
            diffs.append(
                {
                    "kind": kind,
                    "change": _change_of(before, after),
                    "before": before,
                    "after": after,
                }
            )
    _record_diffs(
        diffs,
        "component",
        "component",
        {c["id"]: c for c in b_request.get("components", [])},
        {c["id"]: c for c in t_request.get("components", [])},
    )
    _membership_diffs(
        diffs,
        "dependency",
        None,
        {(d["from"], d["to"]) for d in b_request.get("dependencies", [])},
        {(d["from"], d["to"]) for d in t_request.get("dependencies", [])},
    )
    _record_diffs(
        diffs, "license-rule", "license",
        b_rules.get("licenses", {}), t_rules.get("licenses", {}),
    )
    _record_diffs(
        diffs, "exception-rule", "exception",
        b_rules.get("exceptions", {}), t_rules.get("exceptions", {}),
    )
    _record_diffs(
        diffs, "obligation-scope", "obligation",
        b_rules.get("obligation_scopes", {}), t_rules.get("obligation_scopes", {}),
    )
    _membership_diffs(
        diffs,
        "combined-work-linkage",
        "linkage",
        set(b_rules.get("combined_work_linkages", [])),
        set(t_rules.get("combined_work_linkages", [])),
    )

    rank = {kind: i for i, kind in enumerate(_KIND_ORDER)}
    diffs.sort(key=lambda d: (rank[d["kind"]], _diff_key(d)))
    width = max(4, len(str(len(diffs))))
    for i, diff in enumerate(diffs):
        diff["id"] = "D%0*d" % (width, i + 1)
    return diffs


# ---------------------------------------------------------------------------
# conclusion signature
# ---------------------------------------------------------------------------

def result_signature(result):
    """Project an assessment result onto the compared conclusion.

    Covers the status, the adopted branches, the artifact and
    per-component obligations and the conflict explanation; incidental
    fields (hashes, search mode, distribution, propagation detail) take
    no part in the comparison.
    """
    sig = {"status": result.get("status")}
    for key in _SIGNATURE_KEYS:
        if key in result:
            sig[key] = result[key]
    components = result.get("components")
    if components is not None:
        sig["components"] = [
            {
                "id": c["id"],
                "licenses": c["licenses"],
                "obligations": c["obligations"],
            }
            for c in components
        ]
    return sig


# ---------------------------------------------------------------------------
# counterfactual rollback
# ---------------------------------------------------------------------------

def _set_entry(mapping, key, value):
    if value is None:
        mapping.pop(key, None)
    else:
        mapping[key] = copy.deepcopy(value)


def _apply_rollback(request, rules, diff):
    """Revert one diff to its baseline side inside *request*/*rules*."""
    kind = diff["kind"]
    before = diff["before"]
    if kind in ("distribution", "product"):
        request[kind] = before
    elif kind == "component":
        request["components"] = [
            c for c in request["components"] if c["id"] != diff["component"]
        ]
        if before is not None:
            request["components"].append(copy.deepcopy(before))
    elif kind == "dependency":
        request["dependencies"] = [
            d
            for d in request["dependencies"]
            if (d["from"], d["to"]) != (diff["from"], diff["to"])
        ]
        if before:
            request["dependencies"].append({"from": diff["from"], "to": diff["to"]})
    elif kind == "license-rule":
        _set_entry(rules.setdefault("licenses", {}), diff["license"], before)
    elif kind == "exception-rule":
        _set_entry(rules.setdefault("exceptions", {}), diff["exception"], before)
    elif kind == "obligation-scope":
        _set_entry(rules.setdefault("obligation_scopes", {}), diff["obligation"], before)
    elif kind == "combined-work-linkage":
        linkages = [
            l for l in rules.get("combined_work_linkages", []) if l != diff["linkage"]
        ]
        if before:
            linkages.append(diff["linkage"])
        rules["combined_work_linkages"] = linkages
    else:  # pragma: no cover - defensive
        raise AssertionError("unknown diff kind %r" % kind)


def _counterfactual_result(request, rules):
    """Re-assess a rolled-back state; ``None`` when the state is illegal.

    An intermediate state is illegal when the rolled-back request or
    rules no longer validate -- e.g. a dependency edge left pointing at
    a reverted component, or a ruleset with no license rules left.
    Illegal states are skipped by the search, never counted as hits.
    """
    try:
        norm = normalize_request(request)
        valid_rules = validate_ruleset(rules)
    except ClientError:
        return None
    return assess_normalized(norm, valid_rules)


def find_minimal_rollback(diffs, t_request, t_rules, t_result, baseline_sig):
    """Return ``(diff ids, counterfactual result)`` of the minimal rollback.

    Complete search over rollback subsets in increasing size and
    lexicographic diff-id order (``itertools.combinations`` over the
    id-sorted diffs yields exactly that order); the first state whose
    conclusion matches the baseline signature is the unique answer.
    Rolling back every diff reproduces the baseline state, so the
    search always terminates with a hit.
    """
    n = len(diffs)
    for size in range(n + 1):
        for combo in combinations(range(n), size):
            if not combo:
                outcome = t_result  # the target state itself
            else:
                request = copy.deepcopy(t_request)
                rules = copy.deepcopy(t_rules)
                for i in combo:
                    _apply_rollback(request, rules, diffs[i])
                outcome = _counterfactual_result(request, rules)
                if outcome is None:
                    continue  # illegal intermediate state: not a hit
            if result_signature(outcome) == baseline_sig:
                return [diffs[i]["id"] for i in combo], outcome
    raise AssertionError("unreachable: full rollback reproduces the baseline")


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def compare_assessments(baseline, target):
    """Compare two persisted assessment rows (request + rules + result).

    Returns the full atomic diff list, the globally minimal rollback
    (attribution) set, a summary of the counterfactual outcome and a
    stable ``comparison_hash``.  When the two conclusions already agree
    the minimal set is empty.
    """
    diffs = build_diffs(
        baseline["request"], baseline["rules"],
        target["request"], target["rules"],
    )
    baseline_sig = result_signature(baseline["result"])
    minimal, outcome = find_minimal_rollback(
        diffs,
        target["request"],
        target["rules"],
        target["result"],
        baseline_sig,
    )
    out = {
        "baseline_id": baseline["id"],
        "target_id": target["id"],
        "baseline_status": baseline["result"].get("status"),
        "target_status": target["result"].get("status"),
        "conclusions_agree": not minimal,
        "diff_count": len(diffs),
        "diffs": diffs,
        "minimal_set": minimal,
        "minimal_size": len(minimal),
        "counterfactual": {
            "status": outcome.get("status"),
            "release": outcome.get("release"),
            "summary_hash": outcome.get("summary_hash"),
            "baseline_summary_hash": baseline["result"].get("summary_hash"),
            "restores_baseline_conclusion": True,
        },
    }
    out["comparison_hash"] = stable_hash(out)
    return out
