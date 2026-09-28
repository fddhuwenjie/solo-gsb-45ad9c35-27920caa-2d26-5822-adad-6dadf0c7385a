"""Obligation deadline scheduling for released (``status == "ok"``) assessments.

Pipeline for one schedule request:

1. normalize/validate the request -- RFC 3339 ``release_at``, IANA
   ``timezone``, weekly ``workdays`` (ISO weekday numbers), ``holidays``
   and one ``{offset, unit, depends_on}`` rule per obligation.  Every
   derived collection is sorted/deduplicated, so the input ordering of
   object keys, workdays, holidays and ``depends_on`` arrays can never
   change the result or the ``schedule_hash``;
2. cross-check the assessment: every artifact obligation of the ``ok``
   result must have a scheduling rule -- a missing rule is a 422 and no
   partial schedule is ever produced.  Extra rules (milestones that are
   not artifact obligations) are allowed and scheduled with an empty
   component list, so ``depends_on`` may reference them;
3. compute each obligation's *raw* deadline from the release moment's
   local date in the given timezone: ``offset`` calendar days
   (``calendar_day``) or business days (``business_day`` -- only workdays
   that are not holidays count, the release date itself never does);
4. tighten prerequisites backwards along the acyclic ``depends_on``
   graph: a dependency must never be due later than its dependents, so
   ``final(o) = min(raw(o), min final(s) over dependents s of o)``;
5. report, per obligation, the raw and final deadline, the covered
   components and the complete dependency chain that caused the
   tightening.

Semantic failures -- missing obligation rules, an unknown timezone, an
unparseable ``release_at``/holiday date, dangling ``depends_on``
references or a dependency cycle -- raise :class:`UnprocessableError`
(HTTP 422) before any computation, so a partial plan is impossible.
"""

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import ClientError, stable_hash
from .fulfillment import UnprocessableError
from .solver import _find_cycles

# Offset units for obligation rules.
UNITS = ("calendar_day", "business_day")

# Cap on a single obligation offset (days).  Realistic offsets are days
# or weeks; the cap only bounds the business-day scan loop.
MAX_OFFSET = 100000

_RFC3339_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt]"
    r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"([Zz]|[+-]\d{2}:\d{2})$"
)
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


# ---------------------------------------------------------------------------
# request normalization
# ---------------------------------------------------------------------------

def _parse_release_at(value):
    """Parse an RFC 3339 timestamp into an aware datetime (HTTP 422)."""
    if not isinstance(value, str):
        raise ClientError("release_at must be an RFC 3339 timestamp string")
    text = value.strip()
    m = _RFC3339_RE.match(text)
    if not m:
        raise UnprocessableError(
            "release_at %r is not a valid RFC 3339 timestamp" % value
        )
    year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
    frac = m.group(7)
    micro = int((frac + "000000")[:6]) if frac else 0
    offset = m.group(8)
    if offset in ("Z", "z"):
        tz = timezone.utc
    else:
        oh, om = int(offset[1:3]), int(offset[4:6])
        if oh > 23 or om > 59:
            raise UnprocessableError(
                "release_at %r has an invalid UTC offset" % value
            )
        delta = timedelta(hours=oh, minutes=om)
        tz = timezone(delta if offset[0] == "+" else -delta)
    try:
        return datetime(year, month, day, hour, minute, second, micro, tz)
    except ValueError as exc:
        raise UnprocessableError(
            "release_at %r is not a valid RFC 3339 timestamp: %s" % (value, exc)
        )


def _parse_holiday(value):
    """Parse a YYYY-MM-DD holiday into a date (HTTP 422)."""
    if not isinstance(value, str):
        raise ClientError("holidays entries must be YYYY-MM-DD date strings")
    m = _DATE_RE.match(value.strip())
    if not m:
        raise UnprocessableError(
            "holiday %r is not a valid YYYY-MM-DD calendar date" % value
        )
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise UnprocessableError(
            "holiday %r is not a valid YYYY-MM-DD calendar date" % value
        )


def normalize_schedule_request(body, artifact_obligations):
    """Validate and canonicalize a schedule request.

    Returns ``(release_at, tz_name, tz, workdays, holidays, rules)`` where
    *workdays* is a set of ISO weekday numbers, *holidays* a set of dates
    and *rules* maps obligation id to ``{"offset", "unit", "depends_on"}``
    with ``depends_on`` sorted and deduplicated.
    """
    if not isinstance(body, dict):
        raise ClientError("request body must be a JSON object")

    release_at = _parse_release_at(body.get("release_at"))

    tz_name = body.get("timezone")
    if not isinstance(tz_name, str) or not tz_name.strip():
        raise ClientError("timezone must be a non-empty IANA timezone name")
    tz_name = tz_name.strip()
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise UnprocessableError("unknown IANA timezone %r" % tz_name)

    raw_workdays = body.get("workdays")
    if not isinstance(raw_workdays, list) or not raw_workdays:
        raise ClientError(
            "workdays must be a non-empty array of ISO weekday numbers "
            "(1=Monday .. 7=Sunday)"
        )
    workdays = set()
    for i, wd in enumerate(raw_workdays):
        if isinstance(wd, bool) or not isinstance(wd, int) or not 1 <= wd <= 7:
            raise ClientError(
                "workdays[%d] must be an integer in 1..7 (1=Monday .. 7=Sunday)" % i
            )
        workdays.add(wd)

    raw_holidays = body.get("holidays", [])
    if not isinstance(raw_holidays, list):
        raise ClientError("holidays must be an array of YYYY-MM-DD date strings")
    holidays = {_parse_holiday(h) for h in raw_holidays}

    raw_rules = body.get("obligations", {})
    if not isinstance(raw_rules, dict):
        raise ClientError(
            "obligations must be an object mapping obligation id to its rule"
        )
    rules = {}
    for ob in sorted(raw_rules):
        if not isinstance(ob, str) or not ob:
            raise ClientError("obligation ids must be non-empty strings")
        where = "obligations[%s]" % ob
        spec = raw_rules[ob]
        if not isinstance(spec, dict):
            raise ClientError("%s must be an object" % where)
        offset = spec.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ClientError("%s.offset must be a non-negative integer" % where)
        if offset > MAX_OFFSET:
            raise ClientError(
                "%s.offset must not exceed %d" % (where, MAX_OFFSET)
            )
        unit = spec.get("unit")
        if unit not in UNITS:
            raise ClientError("%s.unit must be one of %s" % (where, list(UNITS)))
        raw_deps = spec.get("depends_on", [])
        if not isinstance(raw_deps, list):
            raise ClientError(
                "%s.depends_on must be an array of obligation ids" % where
            )
        deps = set()
        for dep in raw_deps:
            if not isinstance(dep, str) or not dep:
                raise ClientError(
                    "%s.depends_on entries must be non-empty obligation ids" % where
                )
            deps.add(dep)
        rules[ob] = {"offset": offset, "unit": unit, "depends_on": sorted(deps)}

    # every artifact obligation of the assessment must be schedulable --
    # a partial plan is never produced
    missing = [o for o in artifact_obligations if o not in rules]
    if missing:
        raise UnprocessableError(
            "missing scheduling rules for artifact obligations: %s"
            % ", ".join(missing)
        )

    dangling = sorted(
        {d for r in rules.values() for d in r["depends_on"] if d not in rules}
    )
    if dangling:
        raise UnprocessableError(
            "depends_on references obligations without rules: %s"
            % ", ".join(dangling)
        )

    edges = [(d, ob) for ob, r in rules.items() for d in r["depends_on"]]
    cycles = _find_cycles(sorted(rules), edges)
    if cycles:
        raise UnprocessableError(
            "obligation dependency cycle detected: %s"
            % "; ".join(" -> ".join(cycle + [cycle[0]]) for cycle in cycles)
        )

    return release_at, tz_name, tz, workdays, holidays, rules


# ---------------------------------------------------------------------------
# deadline computation
# ---------------------------------------------------------------------------

def _add_business_days(start, count, workdays, holidays):
    """Advance *count* business days strictly after *start*.

    A day counts when its ISO weekday is in *workdays* and it is not a
    holiday.  *start* itself is never counted: offset 0 returns *start*
    unchanged, offset 1 is the next business day after it.
    """
    d = start
    remaining = count
    while remaining:
        d += timedelta(days=1)
        if d.isoweekday() in workdays and d not in holidays:
            remaining -= 1
    return d


def _tighten(rules, raw):
    """Propagate deadlines backwards over the acyclic depends_on graph.

    A dependency (prerequisite) must never be due later than any of its
    dependents: ``final(o) = min(raw(o), min final(s))`` over the
    obligations *s* that declare ``o`` in their ``depends_on``.  Returns
    ``(final, chains)`` where *chains[o]* is the complete dependency path
    ``[o, s1, ..., sk]`` whose tail *sk* is the obligation whose raw
    deadline ultimately bounds *o* (``[o]`` itself when nothing tighter
    applies).  Ties between equally tight dependents resolve to the
    lexicographically smallest obligation id, keeping the chain (and the
    hash) independent of any input ordering.
    """
    dependents = {ob: [] for ob in rules}
    for ob, r in rules.items():
        for dep in r["depends_on"]:
            dependents[dep].append(ob)
    for ob in dependents:
        dependents[ob].sort()

    final = {}
    chains = {}
    for root in sorted(rules):
        if root in final:
            continue
        # iterative post-order: dependents are finalized before the
        # obligations they depend on (the graph is acyclic by validation)
        stack = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if node in final:
                continue
            if not expanded:
                stack.append((node, True))
                for s in dependents[node]:
                    if s not in final:
                        stack.append((s, False))
                continue
            best_date = raw[node]
            best_chain = [node]
            for s in dependents[node]:  # sorted: first tie-win is canonical
                if final[s] < best_date:
                    best_date = final[s]
                    best_chain = [node] + chains[s]
            final[node] = best_date
            chains[node] = best_chain
    return final, chains


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def build_schedule(body, result, assessment_id):
    """Build the obligation schedule for an ``ok`` assessment.

    The caller guarantees ``result["status"] == "ok"``; the gate lives in
    the API layer so a non-release assessment never reaches this code.
    Any semantic failure raises before computation, so the response is
    always a complete schedule -- never a partial one.
    """
    artifact_obligations = sorted(result.get("artifact_obligations", []))
    release_at, tz_name, tz, workdays, holidays, rules = normalize_schedule_request(
        body, artifact_obligations
    )

    components_by_ob = {o: [] for o in artifact_obligations}
    for crep in result.get("components", []):
        for ob in crep.get("obligations", []):
            components_by_ob.setdefault(ob, []).append(crep["id"])
    for ob in components_by_ob:
        components_by_ob[ob].sort()

    release_date = release_at.astimezone(tz).date()

    raw = {}
    for ob, r in rules.items():
        if r["unit"] == "calendar_day":
            raw[ob] = release_date + timedelta(days=r["offset"])
        else:
            raw[ob] = _add_business_days(
                release_date, r["offset"], workdays, holidays
            )

    final, chains = _tighten(rules, raw)

    items = []
    for ob in sorted(rules):
        r = rules[ob]
        tightened = final[ob] != raw[ob]
        items.append(
            {
                "obligation": ob,
                "components": components_by_ob.get(ob, []),
                "offset": r["offset"],
                "unit": r["unit"],
                "depends_on": r["depends_on"],
                "raw_deadline": raw[ob].isoformat(),
                "deadline": final[ob].isoformat(),
                "tightened": tightened,
                "tightening_chain": chains[ob] if tightened else [],
            }
        )

    out = {
        "assessment_id": assessment_id,
        "release_at": release_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "timezone": tz_name,
        "release_date": release_date.isoformat(),
        "workdays": sorted(workdays),
        "holidays": sorted(h.isoformat() for h in holidays),
        "schedule": items,
    }
    out["schedule_hash"] = stable_hash(out)
    return out
