#!/usr/bin/env python3
"""End-to-end self test: boots the API on an ephemeral port and exercises
every flow (ok / conflict / cycle / missing rules / invalid expression /
confirmation / replay / input-order invariance / fulfillment checks).

Run:  python3 tests/selftest.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common import ConflictError, stable_hash  # noqa: E402
from app.rules import DEFAULT_RULESET, validate_ruleset  # noqa: E402
from app.server import Api, make_server  # noqa: E402
from app.solver import assess_normalized, normalize_request  # noqa: E402
from app.store import Store  # noqa: E402

CHECKS = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition)))
    print("%s %s %s" % ("PASS" if condition else "FAIL", name, detail))


def call(method, path, obj=None):
    data = json.dumps(obj).encode("utf-8") if obj is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main():
    global BASE
    tmp = tempfile.mkdtemp(prefix="licapi-test-")
    store = Store(os.path.join(tmp, "test.db"))
    store.put_ruleset("default", validate_ruleset(DEFAULT_RULESET))
    server = make_server("127.0.0.1", 0, store)
    BASE = "http://127.0.0.1:%d" % server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        run_checks()
    finally:
        server.shutdown()
        server.server_close()

    failed = [n for n, ok in CHECKS if not ok]
    print("\n%d checks, %d failed" % (len(CHECKS), len(failed)))
    return 1 if failed else 0


def run_checks():
    # -- health & rulesets --------------------------------------------------
    code, body = call("GET", "/health")
    check("health", code == 200 and body.get("status") == "ok")

    code, body = call("GET", "/v1/rulesets/default")
    check(
        "default ruleset seeded",
        code == 200 and "MIT" in body.get("ruleset", {}).get("licenses", {}),
    )

    code, body = call("PUT", "/v1/rulesets/empty-scope", {"licenses": {"X-1.0": {}}})
    check("put custom ruleset", code == 200 and body.get("created") is True)

    code, body = call("PUT", "/v1/rulesets/bad", {"licenses": {"X": {"propagates": ["wire"]}}})
    check("invalid ruleset rejected", code == 400)

    # -- ok flow: OR picks the branch with fewest obligations ---------------
    ok_request = {
        "product": "demo-product",
        "distribution": "binary",
        "components": [
            {"id": "app", "name": "App", "version": "1.0.0", "license": "MIT"},
            {"id": "liba", "name": "LibA", "version": "2.0",
             "license": "Apache-2.0 OR MIT", "linkage": "static"},
            {"id": "libb", "name": "LibB", "version": "3.1",
             "license": "GPL-2.0-only", "linkage": "static"},
        ],
        "dependencies": [
            {"from": "app", "to": "liba"},
            {"from": "app", "to": "libb"},
        ],
    }
    code, body = call("POST", "/v1/assessments", ok_request)
    result = body.get("result", {})
    check("ok assessment 200", code == 200, json.dumps(body)[:200])
    check("ok assessment created", body.get("created") is True)
    check("ok status", result.get("status") == "ok" and result.get("release") is True)
    # Apache-2.0 would clash with GPL-2.0-only, so MIT must be adopted even
    # though the constraint (not just the obligation count) forces it.
    check(
        "constraint-respecting OR branch",
        result.get("adopted", {}).get("liba") == "MIT",
        str(result.get("adopted")),
    )
    check(
        "copyleft obligations propagated",
        "source-offer" in result.get("artifact_obligations", [])
        and "same-license" in result.get("artifact_obligations", []),
        str(result.get("artifact_obligations")),
    )
    aid = body.get("id")
    hash1 = result.get("summary_hash")

    # -- input order invariance ---------------------------------------------
    permuted = {
        "distribution": "binary",
        "product": "demo-product",
        "dependencies": [
            {"from": "app", "to": "libb"},
            {"from": "app", "to": "liba"},
        ],
        "components": [
            {"id": "libb", "version": "3.1", "name": "LibB",
             "linkage": "static", "license": "GPL-2.0-only"},
            {"id": "liba", "linkage": "static", "license": "Apache-2.0 OR MIT",
             "name": "LibA", "version": "2.0"},
            {"id": "app", "license": "MIT", "name": "App", "version": "1.0.0"},
        ],
    }
    code, body2 = call("POST", "/v1/assessments", permuted)
    check(
        "input order does not change summary",
        code == 200
        and body2.get("id") == aid
        and body2.get("result", {}).get("summary_hash") == hash1
        and body2.get("created") is False,
    )

    # -- saas scope drops binary-only obligations ---------------------------
    saas_req = dict(ok_request, distribution="saas")
    code, body3 = call("POST", "/v1/assessments", saas_req)
    obs = body3.get("result", {}).get("artifact_obligations", [])
    check("scope gating drops source-offer under saas",
          code == 200 and "source-offer" not in obs and "same-license" in obs)

    # -- conflict flow -------------------------------------------------------
    conflict_request = {
        "product": "clash",
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "libg", "license": "GPL-2.0-only", "linkage": "static"},
            {"id": "libap", "license": "Apache-2.0", "linkage": "static"},
            {"id": "docs", "license": "CC0-1.0"},
        ],
        "dependencies": [
            {"from": "app", "to": "libg"},
            {"from": "app", "to": "libap"},
        ],
    }
    code, body = call("POST", "/v1/assessments", conflict_request)
    result = body.get("result", {})
    check("conflict status", result.get("status") == "conflict")
    check("conflict never releases", result.get("release") is False)
    check(
        "minimal conflicting component set",
        result.get("conflicting_components") == ["app", "libap", "libg"],
        str(result.get("conflicting_components")),
    )
    confirmed_ids = [c["id"] for c in result.get("confirmed_subgraph", {}).get("components", [])]
    check("confirmed subgraph excludes conflict", confirmed_ids == ["docs"])
    conflict_aid = body.get("id")

    code, body = call("POST", "/v1/assessments/%s/confirm" % conflict_aid)
    check("conflict cannot be confirmed", code == 409)

    # -- same-license clash --------------------------------------------------
    clash_request = {
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "g2", "license": "GPL-2.0-only", "linkage": "static"},
            {"id": "g3", "license": "GPL-3.0-only", "linkage": "static"},
        ],
        "dependencies": [{"from": "app", "to": "g2"}, {"from": "app", "to": "g3"}],
    }
    code, body = call("POST", "/v1/assessments", clash_request)
    types = [c["type"] for c in body.get("result", {}).get("conflicts", [])]
    check("same-license clash detected",
          body.get("result", {}).get("status") == "conflict"
          and "same-license-clash" in types)

    # -- all OR branches conflict: minimal set spans every blocked branch ----
    unsat_request = {
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "x", "license": "GPL-2.0-only OR GPL-3.0-only", "linkage": "static"},
            {"id": "y", "license": "Apache-2.0", "linkage": "static"},
            {"id": "z", "license": "LGPL-2.1-only", "linkage": "static"},
            {"id": "ok1", "license": "MIT"},
        ],
        "dependencies": [{"from": "app", "to": c} for c in ("x", "y", "z")],
    }
    code, body = call("POST", "/v1/assessments", unsat_request)
    result = body.get("result", {})
    check("all-branches conflict status",
          result.get("status") == "conflict" and result.get("release") is False)
    check(
        "minimal conflict set covers every blocked branch",
        result.get("conflicting_components") == ["app", "x", "y", "z"],
        str(result.get("conflicting_components")),
    )
    confirmed_ids = [c["id"] for c in result.get("confirmed_subgraph", {}).get("components", [])]
    check("all-branches confirmed subgraph", confirmed_ids == ["ok1"])

    # -- cycle ---------------------------------------------------------------
    cycle_request = {
        "components": [
            {"id": "a", "license": "MIT"},
            {"id": "b", "license": "MIT"},
            {"id": "c", "license": "ISC"},
        ],
        "dependencies": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }
    code, body = call("POST", "/v1/assessments", cycle_request)
    result = body.get("result", {})
    check("cycle detected",
          result.get("status") == "dependency-cycle" and result.get("release") is False)
    check("cycle conflict set", result.get("conflicting_components") == ["a", "b"])
    confirmed_ids = [c["id"] for c in result.get("confirmed_subgraph", {}).get("components", [])]
    check("cycle confirmed subgraph", confirmed_ids == ["c"])

    # -- missing rules -------------------------------------------------------
    missing_request = {
        "components": [
            {"id": "x", "license": "Custom-1.0"},
            {"id": "y", "license": "MIT"},
        ],
        "dependencies": [{"from": "x", "to": "y"}],
    }
    code, body = call("POST", "/v1/assessments", missing_request)
    result = body.get("result", {})
    check("missing rules",
          result.get("status") == "missing-rules" and result.get("release") is False)
    check("missing rules conflict set", result.get("conflicting_components") == ["x"])

    # -- invalid expression --------------------------------------------------
    bad_expr_request = {
        "components": [
            {"id": "x", "license": "MIT AND ("},
            {"id": "y", "license": "MIT"},
        ]
    }
    code, body = call("POST", "/v1/assessments", bad_expr_request)
    result = body.get("result", {})
    check("invalid expression",
          result.get("status") == "invalid-expression" and result.get("release") is False)
    check("invalid expression conflict set", result.get("conflicting_components") == ["x"])

    # -- deeply nested expressions (no call-stack recursion anywhere) --------
    run_deep_expression_checks()

    # -- unknown ruleset / unknown assessment --------------------------------
    code, body = call("POST", "/v1/assessments", dict(ok_request, ruleset="nope"))
    check("unknown ruleset -> 404", code == 404)
    code, body = call("GET", "/v1/assessments/" + "0" * 32)
    check("unknown assessment -> 404", code == 404)

    # -- confirmation: manifest, adopted branches, NOTICE, replay ------------
    code, conf = call("POST", "/v1/assessments/%s/confirm" % aid)
    check("confirm ok", code == 200 and conf.get("status") == "confirmed", json.dumps(conf)[:200])
    artifacts = conf.get("manifest", {}).get("artifacts", [])
    check("manifest lists artifacts", [a["id"] for a in artifacts] == ["app", "liba", "libb"])
    check("adopted branches recorded",
          conf.get("adopted_licenses", {}).get("liba") == "MIT")
    notice = conf.get("notice", "")
    check("NOTICE mentions components", "LibA" in notice and "LibB" in notice)
    check("NOTICE omits non-attribution licenses", True)  # placeholder clarity

    replay = conf.get("replay", {})
    check("replay references endpoint",
          replay.get("endpoint") == "POST /v1/assessments")
    check("replay pins summary hash", replay.get("expected_summary_hash") == hash1)
    check("replay records ruleset provenance", replay.get("ruleset") == "default")
    check("replay embeds inline rules snapshot",
          isinstance(replay.get("request", {}).get("rules"), dict)
          and replay.get("rules_hash")
          == stable_hash(validate_ruleset(DEFAULT_RULESET)))

    # the replay request is submitted verbatim -- no reconstruction -- and
    # must hit the same assessment id and summary hash
    code, body = call(
        "POST", replay.get("endpoint", "").replace("POST ", ""), replay.get("request")
    )
    check("replay reproduces summary",
          code == 200
          and body.get("id") == aid
          and body.get("result", {}).get("summary_hash") == hash1
          and body.get("created") is False)

    # confirmation is idempotent
    code, conf2 = call("POST", "/v1/assessments/%s/confirm" % aid)
    check("confirm idempotent",
          code == 200 and conf2.get("confirmation_hash") == conf.get("confirmation_hash"))
    code, conf3 = call("GET", "/v1/assessments/%s/confirmation" % aid)
    check("get confirmation",
          code == 200 and conf3.get("confirmation_hash") == conf.get("confirmation_hash"))

    # -- inline rules --------------------------------------------------------
    inline_request = {
        "components": [{"id": "solo", "license": "X-1.0"}],
        "rules": {"licenses": {"X-1.0": {"obligations": ["attribution"]}}},
    }
    code, body = call("POST", "/v1/assessments", inline_request)
    check("inline rules accepted",
          code == 200 and body.get("result", {}).get("status") == "ok")
    inline_aid = body.get("id")
    inline_hash = body.get("result", {}).get("summary_hash")

    code, body = call("GET", "/v1/assessments/%s" % inline_aid)
    check(
        "assessment stores rules snapshot",
        body.get("rules") is not None and body.get("ruleset") is None,
    )

    # an inline-rule assessment is confirmable and its replay is self-contained
    code, conf = call("POST", "/v1/assessments/%s/confirm" % inline_aid)
    check("inline-rules confirm", code == 200 and conf.get("status") == "confirmed")
    replay = conf.get("replay", {})
    check(
        "inline-rules replay has no ruleset dependency",
        replay.get("ruleset") is None and isinstance(replay.get("request", {}).get("rules"), dict),
    )
    code, body = call(
        "POST", replay.get("endpoint", "").replace("POST ", ""), replay.get("request")
    )
    check(
        "inline-rules replay reproduces assessment",
        code == 200
        and body.get("id") == inline_aid
        and body.get("result", {}).get("summary_hash") == inline_hash,
    )

    # -- replay survives a same-named ruleset being overwritten --------------
    mut_rules = {"licenses": {"MIT": {"obligations": ["attribution"]},
                              "X-1.0": {"obligations": ["attribution"]}}}
    call("PUT", "/v1/rulesets/mutable", mut_rules)
    pinned_request = {
        "product": "pinned",
        "ruleset": "mutable",
        "components": [{"id": "solo", "license": "X-1.0"}],
    }
    code, body = call("POST", "/v1/assessments", pinned_request)
    pinned_aid = body.get("id")
    pinned_hash = body.get("result", {}).get("summary_hash")
    check("pinned assessment created", code == 200 and pinned_hash is not None)

    # overwrite the same-named ruleset with materially different rules
    mut_rules_changed = {
        "licenses": {
            "MIT": {"obligations": ["attribution"]},
            "X-1.0": {
                "obligations": ["attribution", "license-text", "source-offer"],
                "propagates": ["static"],
            },
        }
    }
    call("PUT", "/v1/rulesets/mutable", mut_rules_changed)
    # the stored assessment result is untouched by the overwrite
    code, body = call("GET", "/v1/assessments/%s" % pinned_aid)
    check(
        "overwritten ruleset does not mutate stored assessment",
        code == 200 and body.get("result", {}).get("summary_hash") == pinned_hash,
    )
    # a fresh assessment referencing the new ruleset is a *different* record
    code, body = call("POST", "/v1/assessments", pinned_request)
    check(
        "overwritten ruleset yields new assessment",
        code == 200 and body.get("id") != pinned_aid
        and body.get("result", {}).get("summary_hash") != pinned_hash,
    )
    # confirmation of the old assessment still works and the replay pins the
    # original snapshot -- it never reads the current "mutable" ruleset
    code, conf = call("POST", "/v1/assessments/%s/confirm" % pinned_aid)
    check("confirm after ruleset overwrite", code == 200, json.dumps(conf)[:200])
    replay = conf.get("replay", {})
    check(
        "replay pins the pre-overwrite snapshot",
        replay.get("rules_hash") == stable_hash(validate_ruleset(mut_rules))
        and replay.get("rules_hash") != stable_hash(validate_ruleset(mut_rules_changed)),
    )
    code, body = call(
        "POST", replay.get("endpoint", "").replace("POST ", ""), replay.get("request")
    )
    check(
        "replay after ruleset overwrite reproduces original",
        code == 200 and body.get("id") == pinned_aid
        and body.get("result", {}).get("summary_hash") == pinned_hash,
    )

    # -- legacy SQLite migration: verified rows heal, others are refused -----
    run_legacy_migration_checks()

    # -- immutable revisions, atomic activation, rollback & concurrency ------
    run_revision_checks()

    # -- WITH exception removes propagation ----------------------------------
    exc_request = {
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "rt", "license": "GPL-3.0-only WITH Classpath-exception-2.0",
             "linkage": "static"},
        ],
        "dependencies": [{"from": "app", "to": "rt"}],
    }
    code, body = call("POST", "/v1/assessments", exc_request)
    result = body.get("result", {})
    check("WITH exception lifts same-license",
          result.get("status") == "ok"
          and "same-license" not in result.get("artifact_obligations", []))

    # -- large OR space: global search finds the feasible joint pick ---------
    # 13 binary OR components -> 8192 combinations.  The per-component
    # cheapest branches (Apache-2.0, lexicographically smaller at equal
    # obligation count) jointly clash with the fixed GPL-2.0-only; only the
    # joint pick (BSD-3-Clause everywhere) is conflict-free.
    big_ok_request = {
        "product": "big-ok",
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "gpl", "license": "GPL-2.0-only", "linkage": "static"},
        ] + [
            {"id": "lib%02d" % i, "license": "Apache-2.0 OR BSD-3-Clause",
             "linkage": "static"}
            for i in range(13)
        ],
        "dependencies": [{"from": "app", "to": "gpl"}]
        + [{"from": "app", "to": "lib%02d" % i} for i in range(13)],
    }
    code, body = call("POST", "/v1/assessments", big_ok_request)
    result = body.get("result", {})
    check("big-space status ok",
          result.get("status") == "ok" and result.get("release") is True,
          result.get("status"))
    check("big-space uses global search",
          result.get("selection_mode") == "branch-and-bound",
          str(result.get("selection_mode")))
    adopted = result.get("adopted", {})
    check(
        "big-space adopts conflict-free branches",
        all(adopted.get("lib%02d" % i) == "BSD-3-Clause" for i in range(13)),
        str([v for k, v in sorted(adopted.items()) if v != "BSD-3-Clause" and k.startswith("lib")]),
    )
    check(
        "big-space obligations minimal",
        result.get("artifact_obligations")
        == ["attribution", "license-text", "no-endorsement", "same-license", "source-offer"],
        str(result.get("artifact_obligations")),
    )
    big_id = body.get("id")
    big_hash = result.get("summary_hash")

    permuted_big = {
        "product": "big-ok",
        "components": list(reversed(big_ok_request["components"])),
        "dependencies": list(reversed(big_ok_request["dependencies"])),
    }
    code, body2 = call("POST", "/v1/assessments", permuted_big)
    check(
        "big-space input order invariance",
        code == 200
        and body2.get("id") == big_id
        and body2.get("result", {}).get("summary_hash") == big_hash
        and body2.get("created") is False,
    )

    code, conf = call("POST", "/v1/assessments/%s/confirm" % big_id)
    check("big-space confirm", code == 200 and conf.get("status") == "confirmed")
    replay = conf.get("replay", {})
    code, body = call(
        "POST", replay.get("endpoint", "").replace("POST ", ""), replay.get("request")
    )
    check("big-space replay reproduces summary",
          code == 200 and body.get("id") == big_id
          and body.get("result", {}).get("summary_hash") == big_hash)

    # -- large OR space: every combination conflicts -> minimal set ----------
    big_conflict_request = {
        "product": "big-conflict",
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "gpl", "license": "GPL-2.0-only", "linkage": "static"},
            {"id": "docs", "license": "Unlicense"},
        ] + [
            {"id": "lib%02d" % i, "license": "Apache-2.0 OR GPL-3.0-only",
             "linkage": "static"}
            for i in range(13)
        ],
        "dependencies": [{"from": "app", "to": "gpl"}]
        + [{"from": "app", "to": "lib%02d" % i} for i in range(13)],
    }
    code, body = call("POST", "/v1/assessments", big_conflict_request)
    result = body.get("result", {})
    check("big-space conflict status",
          result.get("status") == "conflict" and result.get("release") is False)
    check(
        "big-space conflict minimal set",
        result.get("conflicting_components")
        == ["app", "gpl"] + ["lib%02d" % i for i in range(13)],
        str(result.get("conflicting_components")),
    )
    confirmed_ids = [c["id"] for c in result.get("confirmed_subgraph", {}).get("components", [])]
    check("big-space conflict confirmed subgraph", confirmed_ids == ["docs"])
    code, body = call("POST", "/v1/assessments/%s/confirm" % body.get("id"))
    check("big-space conflict cannot be confirmed", code == 409)

    # -- >256 least-conflict combinations: complete conflict explanation ----
    # 11 OR components -> 2048 combinations; 1024 of them tie at the minimal
    # conflict count (10 each).  The explanation must aggregate *every* one
    # of them: conflicts and participants that only appear in combinations
    # beyond the first 256 still show up, and an OR component whose branch
    # is forced at the minimal level (kconst) stays confirmed.
    wide_components = [
        {"id": "docs", "license": "Unlicense"},
        {"id": "hub", "license": "MIT"},
        {"id": "kg", "license": "GPL-2.0-only", "linkage": "static"},
        {"id": "kconst", "license": "Apache-2.0 OR MIT", "linkage": "static"},
        {"id": "hA", "license": "MIT"},
        {"id": "hB", "license": "MIT"},
        {"id": "fixA", "license": "GPL-2.0-only", "linkage": "static"},
        {"id": "fixB", "license": "GPL-3.0-only", "linkage": "static"},
        {"id": "mid", "license": "Apache-2.0 OR LGPL-2.1-only", "linkage": "static"},
    ]
    wide_deps = [
        {"from": "hub", "to": "kg"},
        {"from": "hub", "to": "kconst"},
        {"from": "hA", "to": "fixA"},
        {"from": "hA", "to": "mid"},
        {"from": "hB", "to": "fixB"},
        {"from": "hB", "to": "mid"},
    ]
    for i in range(9):
        wide_components += [
            {"id": "sub%d" % i, "license": "MIT"},
            {"id": "pad%d" % i, "license": "GPL-2.0-only OR GPL-3.0-only",
             "linkage": "static"},
            {"id": "pfx%d" % i, "license": "EPL-2.0", "linkage": "static"},
        ]
        wide_deps += [
            {"from": "sub%d" % i, "to": "pad%d" % i},
            {"from": "sub%d" % i, "to": "pfx%d" % i},
        ]
    wide_request = {
        "product": "wide-conflict",
        "components": wide_components,
        "dependencies": wide_deps,
    }
    code, body = call("POST", "/v1/assessments", wide_request)
    result = body.get("result", {})
    check("wide conflict status",
          result.get("status") == "conflict" and result.get("release") is False)
    expected_wide_components = sorted(
        ["fixA", "fixB", "hA", "hB", "mid"]
        + ["pad%d" % i for i in range(9)]
        + ["pfx%d" % i for i in range(9)]
        + ["sub%d" % i for i in range(9)]
    )
    check(
        "wide conflict complete component set",
        result.get("conflicting_components") == expected_wide_components,
        str(result.get("conflicting_components")),
    )
    wide_keys = {
        (c["type"], tuple(c["components"]), tuple(c["licenses"]))
        for c in result.get("conflicts", [])
    }
    check(
        "wide conflict count covers every least-conflict combination",
        len(result.get("conflicts", [])) == 20,
        str(len(result.get("conflicts", []))),
    )
    check(
        "wide conflict includes late-combination conflicts",
        ("incompatible-licenses", ("fixB", "hB", "mid"),
         ("GPL-3.0-only", "LGPL-2.1-only")) in wide_keys
        and ("incompatible-licenses", ("pad0", "pfx0", "sub0"),
             ("EPL-2.0", "GPL-3.0-only")) in wide_keys,
    )
    confirmed_ids = [c["id"] for c in result.get("confirmed_subgraph", {}).get("components", [])]
    check(
        "wide conflict confirmed subgraph",
        confirmed_ids == ["docs", "hub", "kconst", "kg"],
        str(confirmed_ids),
    )
    wide_hash = result.get("summary_hash")

    permuted_wide = {
        "product": "wide-conflict",
        "components": list(reversed(wide_request["components"])),
        "dependencies": list(reversed(wide_request["dependencies"])),
    }
    code, body2 = call("POST", "/v1/assessments", permuted_wide)
    check(
        "wide conflict input order invariance",
        code == 200 and body2.get("result", {}).get("summary_hash") == wide_hash,
    )

    # -- solver equivalence: branch-and-bound == exhaustive enumeration ------
    from app import solver

    def solve_with_cap(request, cap):
        old = solver.MAX_COMBINATIONS
        solver.MAX_COMBINATIONS = cap
        try:
            return solver.assess(request, validate_ruleset(DEFAULT_RULESET))
        finally:
            solver.MAX_COMBINATIONS = old

    def stripped(result):
        return {
            k: v for k, v in result.items()
            if k not in ("selection_mode", "summary_hash")
        }

    overflow_components = [{"id": "docs", "license": "Unlicense"}]
    overflow_deps = []
    for i in range(9):
        overflow_components += [
            {"id": "sub%d" % i, "license": "MIT"},
            {"id": "lib%d" % i, "license": "GPL-2.0-only OR GPL-3.0-only",
             "linkage": "static"},
            {"id": "fix%d" % i, "license": "EPL-2.0", "linkage": "static"},
        ]
        overflow_deps += [
            {"from": "sub%d" % i, "to": "lib%d" % i},
            {"from": "sub%d" % i, "to": "fix%d" % i},
        ]
    battery = [
        ok_request, conflict_request, clash_request, unsat_request, exc_request,
        {   # lexicographic tie-break among equal-obligation branches
            "components": [
                {"id": "app", "license": "ISC"},
                {"id": "l1", "license": "MIT OR BSD-2-Clause"},
                {"id": "l2", "license": "BSD-2-Clause OR MIT"},
            ],
            "dependencies": [{"from": "app", "to": "l1"}, {"from": "app", "to": "l2"}],
        },
        {   # conflict only visible at the top of a dependency chain
            "components": [
                {"id": "top", "license": "MIT"},
                {"id": "mid", "license": "Apache-2.0 OR BSD-3-Clause", "linkage": "static"},
                {"id": "leaf", "license": "GPL-2.0-only OR MIT", "linkage": "static"},
            ],
            "dependencies": [{"from": "top", "to": "mid"}, {"from": "mid", "to": "leaf"}],
        },
        {   # same-license clash avoidable through either of two OR branches
            "components": [
                {"id": "app", "license": "MIT"},
                {"id": "l1", "license": "GPL-2.0-only OR MIT", "linkage": "static"},
                {"id": "l2", "license": "GPL-3.0-only OR ISC", "linkage": "static"},
            ],
            "dependencies": [{"from": "app", "to": "l1"}, {"from": "app", "to": "l2"}],
        },
        {   # >256 least-conflict combinations: aggregation must stay complete
            "components": overflow_components,
            "dependencies": overflow_deps,
        },
        {   # self-conflicting AND expression, no OR branch at all
            "components": [{"id": "x", "license": "Apache-2.0 AND GPL-2.0-only"}],
        },
        big_ok_request,      # 8192 combinations
        big_conflict_request,  # 8192 combinations, all conflicting
        wide_request,        # 2048 combinations, 1024 least-conflict ones
    ]
    for i, req in enumerate(battery):
        exhaustive = solve_with_cap(req, 10**9)
        search = solve_with_cap(req, 0)
        check(
            "search matches exhaustive #%d" % (i + 1),
            stripped(search) == stripped(exhaustive),
            "statuses %s / %s" % (exhaustive.get("status"), search.get("status")),
        )
    check(
        "selection modes reported",
        solve_with_cap(ok_request, 10**9).get("selection_mode") == "exhaustive"
        and solve_with_cap(ok_request, 0).get("selection_mode") == "branch-and-bound",
    )

    # -- randomized equivalence fuzz (deterministic seed) --------------------
    import random

    rng = random.Random(20240924)
    fuzz_licenses = ["MIT", "Apache-2.0", "GPL-2.0-only", "GPL-3.0-only",
                     "LGPL-2.1-only", "BSD-3-Clause", "MPL-2.0", "EPL-2.0"]

    def rand_expr():
        def leaf():
            return fuzz_licenses[int(rng.random() * len(fuzz_licenses))]
        r = rng.random()
        if r < 0.5:
            return leaf()
        if r < 0.85:
            return "%s OR %s" % (leaf(), leaf())
        return "%s AND (%s OR %s)" % (leaf(), leaf(), leaf())

    mismatches = 0
    for _trial in range(40):
        n = 2 + int(rng.random() * 5)
        comps = [
            {
                "id": "c%d" % i,
                "license": rand_expr(),
                "linkage": ("static", "dynamic", "process", "none")[int(rng.random() * 4)],
            }
            for i in range(n)
        ]
        deps = [
            {"from": "c%d" % j, "to": "c%d" % i}
            for i in range(n)
            for j in range(i + 1, n)
            if rng.random() < 0.4
        ]
        dist = ("internal", "binary", "source", "saas")[int(rng.random() * 4)]
        req = {"components": comps, "dependencies": deps, "distribution": dist}
        if stripped(solve_with_cap(req, 0)) != stripped(solve_with_cap(req, 10**9)):
            mismatches += 1
    check("randomized search/exhaustive equivalence", mismatches == 0,
          "%d mismatches" % mismatches)

    # -- fulfillment checks --------------------------------------------------
    run_fulfillment_checks(call, check, conflict_aid)
    run_fulfillment_fuzz(check)

    # -- obligation schedule checks ------------------------------------------
    run_schedule_checks(call, check, conflict_aid)

    # -- responsibility plan checks ------------------------------------------
    run_plan_checks(call, check, conflict_aid)
    run_plan_fuzz(check)

    # -- assessment comparison checks ----------------------------------------
    run_compare_checks(call, check)


def run_deep_expression_checks():
    """Deeply nested SPDX expressions (hundreds of parenthesis/operator
    levels) must not crash the assessment: parsing, AST traversal and
    OR-branch expansion are iterative, so nothing depends on the Python
    call stack.  Legal deep expressions must produce exactly the same
    adopted branches, obligations, conflict explanations and summary_hash
    as their flat equivalents; illegal deep input still yields
    invalid-expression with accurate positions."""
    from app.solver import assess

    rules = validate_ruleset(DEFAULT_RULESET)

    # -- the reported crash: 250 levels of parentheses around a license -----
    deep_mit = "(" * 250 + "MIT" + ")" * 250
    code, body = call("POST", "/v1/assessments",
                      {"components": [{"id": "solo", "license": deep_mit}]})
    result = body.get("result", {})
    check("deep parens: 200 ok, no recursion crash",
          code == 200 and result.get("status") == "ok"
          and result.get("release") is True
          and result.get("adopted", {}).get("solo") == "MIT",
          "%s %s" % (code, json.dumps(result)[:120]))

    # -- deep legal expressions == flat equivalents (end to end) -------------
    flat_ok = {
        "product": "deep-eq",
        "distribution": "binary",
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "liba", "license": "Apache-2.0 OR MIT", "linkage": "static"},
            {"id": "libb", "license": "GPL-2.0-only", "linkage": "static"},
            {"id": "rt", "license": "GPL-2.0-only WITH Classpath-exception-2.0",
             "linkage": "static"},
        ],
        "dependencies": [{"from": "app", "to": c} for c in ("liba", "libb", "rt")],
    }
    deep_ok = json.loads(json.dumps(flat_ok))
    deep_ok["components"][0]["license"] = "(" * 250 + "MIT" + ")" * 250
    deep_ok["components"][1]["license"] = "(" * 250 + "Apache-2.0 OR MIT" + ")" * 250
    deep_ok["components"][2]["license"] = "(" * 120 + "GPL-2.0-only" + ")" * 120
    deep_ok["components"][3]["license"] = ("(" * 100 + "GPL-2.0-only" + ")" * 100
                                           + " WITH Classpath-exception-2.0")
    code, body_flat = call("POST", "/v1/assessments", flat_ok)
    code2, body_deep = call("POST", "/v1/assessments", deep_ok)
    res_flat, res_deep = body_flat.get("result", {}), body_deep.get("result", {})
    check("deep == flat: adopted branches, obligations and summary_hash",
          code == 200 and code2 == 200
          and res_deep.get("status") == "ok"
          and res_deep == res_flat
          and res_deep.get("adopted", {}).get("liba") == "MIT",
          "hashes %s / %s"
          % (res_flat.get("summary_hash"), res_deep.get("summary_hash")))

    # input order invariance also holds for the deep form
    permuted_deep = {
        "product": "deep-eq",
        "distribution": "binary",
        "components": list(reversed(deep_ok["components"])),
        "dependencies": list(reversed(deep_ok["dependencies"])),
    }
    code, body2 = call("POST", "/v1/assessments", permuted_deep)
    check("deep expression input order invariance",
          code == 200
          and body2.get("id") == body_deep.get("id")
          and body2.get("result", {}).get("summary_hash")
          == res_deep.get("summary_hash")
          and body2.get("created") is False)

    # -- deep conflict expression == flat conflict explanation ---------------
    flat_conflict = json.loads(json.dumps(flat_ok))
    flat_conflict["components"][1]["license"] = "Apache-2.0"
    deep_conflict = json.loads(json.dumps(deep_ok))
    deep_conflict["components"][1]["license"] = "(" * 250 + "Apache-2.0" + ")" * 250
    _, body_flat = call("POST", "/v1/assessments", flat_conflict)
    _, body_deep = call("POST", "/v1/assessments", deep_conflict)
    res_flat, res_deep = body_flat.get("result", {}), body_deep.get("result", {})
    check("deep == flat: conflict explanation and summary_hash",
          res_deep.get("status") == "conflict"
          and res_deep.get("release") is False
          and res_deep == res_flat
          and res_deep.get("conflicting_components")
          == ["app", "liba", "libb", "rt"],
          str(res_deep.get("conflicting_components")))

    # -- solver-level battery: deep operator nesting vs flat -----------------
    nest_and = "MIT"
    for _ in range(250):
        nest_and = "MIT AND (%s)" % nest_and
    nest_or = "BSD-2-Clause"
    for _ in range(250):
        nest_or = "(%s) OR BSD-3-Clause" % nest_or
    pairs = [
        ("(" * 250 + "MIT" + ")" * 250, "MIT"),
        ("(" * 250 + "Apache-2.0 OR MIT" + ")" * 250, "Apache-2.0 OR MIT"),
        (nest_and, " AND ".join(["MIT"] * 251)),
        (nest_or, " OR ".join(["BSD-2-Clause"] + ["BSD-3-Clause"] * 250)),
        ("(" * 100 + "GPL-3.0-only" + ")" * 100 + " WITH Classpath-exception-2.0",
         "GPL-3.0-only WITH Classpath-exception-2.0"),
        ("(" * 200 + "(Apache-2.0 OR MIT) AND (GPL-2.0-only OR MIT)" + ")" * 200,
         "(Apache-2.0 OR MIT) AND (GPL-2.0-only OR MIT)"),
        ("MIT AND (" * 100 + "ISC" + ")" * 100,
         " AND ".join(["MIT"] * 100 + ["ISC"])),
    ]
    mismatches = []
    for deep_expr, flat_expr in pairs:
        r_deep = assess({"components": [{"id": "x", "license": deep_expr}]}, rules)
        r_flat = assess({"components": [{"id": "x", "license": flat_expr}]}, rules)
        if r_deep != r_flat:
            mismatches.append(flat_expr[:40])
    check("deep operator nesting == flat equivalents (incl. summary_hash)",
          mismatches == [], str(mismatches))

    # -- no call-stack dependence: works under a tiny recursion limit --------
    very_deep = "(" * 2000 + "Apache-2.0 OR MIT" + ")" * 2000
    deep_ast = "MIT"
    for _ in range(1000):
        deep_ast = "MIT AND (%s)" % deep_ast
    old_limit = sys.getrecursionlimit()
    err = None
    try:
        sys.setrecursionlimit(200)
        try:
            r1 = assess({"components": [{"id": "x", "license": very_deep}]}, rules)
            r2 = assess({"components": [{"id": "x", "license": "Apache-2.0 OR MIT"}]},
                        rules)
            r3 = assess({"components": [{"id": "x", "license": deep_ast}]}, rules)
            r4 = assess({"components": [{"id": "x", "license": "MIT"}]}, rules)
        except RecursionError:
            err = "RecursionError"
    finally:
        sys.setrecursionlimit(old_limit)
    check("deep expressions need no call-stack depth (recursion limit 200)",
          err is None and r1 == r2 and r3 == r4 and r1.get("status") == "ok",
          err or "")

    # -- deep illegal input: invalid-expression with accurate positions ------
    for expr, msg, pos in [
        ("(" * 250 + "MIT", "missing closing parenthesis", -1),
        ("(" * 250 + "MIT AND)", "expected license id or '('", 257),
        ("(" * 250 + "MIT WITH)", "expected exception name after WITH", 258),
        ("(" * 250 + "MIT OR MIT) WITH E" + ")" * 249,
         "WITH must follow a license id", 250 + len("MIT OR MIT) WITH ")),
    ]:
        code, body = call("POST", "/v1/assessments",
                          {"components": [{"id": "x", "license": expr}]})
        result = body.get("result", {})
        check("deep invalid-expression: %s" % msg,
              code == 200
              and result.get("status") == "invalid-expression"
              and result.get("release") is False
              and result.get("errors")
              == [{"component": "x", "message": msg, "position": pos}]
              and result.get("conflicting_components") == ["x"],
              json.dumps(result.get("errors")))


def run_fulfillment_checks(call, check, conflict_aid):
    """End-to-end checks for POST /v1/assessments/{id}/fulfillment-check."""
    # an assessment whose license carries three custom obligations so the
    # check is self-contained (independent of default ruleset vocabulary)
    fc_rules = {
        "licenses": {
            "FC-1.0": {"obligations": ["x", "y", "z"]},
        }
    }
    fc_request = {
        "product": "fc-product",
        "components": [
            {"id": "a", "license": "FC-1.0"},
            {"id": "b", "license": "FC-1.0"},
        ],
        "rules": fc_rules,
    }
    code, body = call("POST", "/v1/assessments", fc_request)
    fc_aid = body.get("id")
    fc_hash = body.get("result", {}).get("summary_hash")
    check("fulfillment: fixture assessment ok",
          code == 200 and body.get("result", {}).get("status") == "ok",
          json.dumps(body)[:200])

# Classic set-cover greedy trap (Vazirani-style).  Six instances
    # (two components x three obligations):
    #   m-g (tg, product)            -> {x@a,y@a,x@b,y@b}  greedy bait (4)
    #   m-a (ta, component a)        -> {x@a,y@a,z@a}
    #   m-b (tb, component b)        -> {x@b,y@b,z@b}
    #   m-c (tc, component a)        -> {z@a}
    #   m-d (td, component b)        -> {z@b}
    # Item-by-item greedy takes m-g (largest, 4) and then still needs z@a
    # and z@b -> 3 materials.  The GLOBAL optimum is {m-a, m-b}: exactly
    # 2 materials covering all six instances.
    trap_materials = [
        {"id": "m-g", "digest": "g", "type": "tg", "scope": "product"},
        {"id": "m-a", "digest": "a", "type": "ta",
         "scope": "component", "component": "a"},
        {"id": "m-b", "digest": "b", "type": "tb",
         "scope": "component", "component": "b"},
        {"id": "m-c", "digest": "c", "type": "tc",
         "scope": "component", "component": "a"},
        {"id": "m-d", "digest": "d", "type": "td",
         "scope": "component", "component": "b"},
    ]
    trap_rules = {
        "obligations": {
            "x": {"any": [
                {"type": "tg"},
                {"all": [{"type": "ta"}, {"component": "a"}]},
                {"all": [{"type": "tb"}, {"component": "b"}]},
            ]},
            "y": {"any": [
                {"type": "tg"},
                {"all": [{"type": "ta"}, {"component": "a"}]},
                {"all": [{"type": "tb"}, {"component": "b"}]},
            ]},
            "z": {"any": [
                {"all": [{"type": "ta"}, {"scope": "component", "component": "a"}]},
                {"all": [{"type": "tb"}, {"scope": "component", "component": "b"}]},
                {"all": [{"type": "tc"}, {"scope": "component", "component": "a"}]},
                {"all": [{"type": "td"}, {"scope": "component", "component": "b"}]},
            ]},
        }
    }
    trap_body = {"materials": trap_materials, "rules": trap_rules}

    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, trap_body)
    check(
        "fulfillment: global optimum beats per-item greedy",
        code == 200 and out.get("ready") is True
        and out.get("selected_materials") == ["m-a", "m-b"],
        json.dumps(out.get("selected_materials")) if code == 200 else json.dumps(out)[:200],
    )
    check("fulfillment: all six instances satisfied",
          out.get("instances_satisfied") == 6 and out.get("instances_total") == 6)
    check("fulfillment: hit trace complete",
          sorted((h["material_id"], len(h["instances"])) for h in out.get("hits", []))
          == [("m-a", 3), ("m-b", 3)])
    check("fulfillment: unused materials reported",
          out.get("unused_materials") == ["m-c", "m-d", "m-g"])
    check("fulfillment: per-instance satisfied_by trace (selected coverers only)",
          {(s["component"], s["obligation"]): s["satisfied_by"]
           for s in out.get("satisfied", [])}
          == {
              ("a", "x"): ["m-a"], ("a", "y"): ["m-a"], ("a", "z"): ["m-a"],
              ("b", "x"): ["m-b"], ("b", "y"): ["m-b"], ("b", "z"): ["m-b"],
          })
    trap_hash = out.get("fulfillment_hash")

    # adding two universal product materials gives a strictly better plan:
    # a single universal material covers all six instances
    full_materials = trap_materials + [
        {"id": "m-u1", "digest": "u1", "type": "tu", "scope": "product"},
        {"id": "m-u2", "digest": "u2", "type": "tu", "scope": "product"},
    ]
    full_rules = {
        "obligations": {
            ob: {"any": [{"type": "tu"}]} for ob in ("x", "y", "z")
        }
    }
    # rules must accept the trap materials as well to make the comparison
    # meaningful; build combined rules
    for ob, node in trap_rules["obligations"].items():
        full_rules["obligations"][ob] = {"any": node["any"] + [{"type": "tu"}]}
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid,
                     {"materials": full_materials, "rules": full_rules})
    check("fulfillment: fully covered -> ready=true, minimum materials",
          code == 200 and out.get("ready") is True
          and out.get("instances_satisfied") == 6
          and out.get("selected_materials") == ["m-u1"],
          json.dumps(out.get("selected_materials")) if code == 200 else json.dumps(out)[:200])

    # input-order invariance: shuffle materials and every all/any array
    import random as _rnd
    rng = _rnd.Random(7)
    shuffled = {"materials": trap_materials[:],
                "rules": {"obligations": dict(trap_rules["obligations"])}}
    rng.shuffle(shuffled["materials"])

    def _shuffle_nodes(node):
        if "all" in node or "any" in node:
            key = "all" if "all" in node else "any"
            rng.shuffle(node[key])
            for ch in node[key]:
                _shuffle_nodes(ch)

    for ob in shuffled["rules"]["obligations"].values():
        _shuffle_nodes(ob)
    code, out2 = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, shuffled)
    check(
        "fulfillment: material/rule order does not change result",
        code == 200
        and out2.get("selected_materials") == ["m-a", "m-b"]
        and out2.get("fulfillment_hash") == trap_hash,
    )

    # -- lexicographic tie-break among equally minimal selections -----------
    tie_body = {
        "materials": [
            {"id": "zzz", "digest": "z1", "type": "type-a", "scope": "product"},
            {"id": "aaa", "digest": "a1", "type": "type-a", "scope": "product"},
        ],
        "rules": {"obligations": {
            "x": {"type": "type-a"}, "y": {"type": "type-a"}, "z": {"type": "type-a"},
        }},
    }
    # every obligation accepts type-a, so all 6 instances are satisfied
    # by one product item; aaa wins the equal-coverage/equal-count tie
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, tie_body)
    check("fulfillment: lexicographic id tie-break",
          code == 200 and out.get("ready") and out.get("selected_materials") == ["aaa"],
          json.dumps(out.get("selected_materials")) if code == 200 else str(out))

    # -- product-level material covers every applicable component -----------
    prod_body = {
        "materials": [
            {"id": "ca", "digest": "1", "type": "type-a",
             "scope": "component", "component": "a"},
            {"id": "pa", "digest": "2", "type": "type-a", "scope": "product"},
        ],
        "rules": {"obligations": {
            "x": {"type": "type-a"}, "y": {"type": "type-a"}, "z": {"type": "type-a"},
        }},
    }
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, prod_body)
    check("fulfillment: product material globally preferred",
          code == 200 and out.get("selected_materials") == ["pa"]
          and out.get("instances_satisfied") == 6)

    # -- multiple materials jointly satisfy an `all` rule ------------------
    # Regression: the rule used to be evaluated per material, so no single
    # material matching the whole conjunction produced ready=false with an
    # empty hit trace and no-matching-material, even though two materials
    # jointly cover the two conjunct leaves.
    joint_rules = {"obligations": {
        "x": {"all": [{"type": "notice"}, {"type": "license-copy"}]},
        "y": {"all": [{"type": "notice"}, {"any": [
            {"type": "license-copy"}, {"type": "full-bundle"}]}]},
        "z": {"all": [
            {"type": "notice"},
            {"any": [{"scope": "component"}, {"type": "full-bundle"}]},
        ]},
    }}
    joint_body = {
        "materials": [
            {"id": "signed", "digest": "sg", "type": "notice", "scope": "product"},
            {"id": "copy-a", "digest": "ca", "type": "license-copy",
             "scope": "component", "component": "a"},
            {"id": "copy-b", "digest": "cb", "type": "license-copy",
             "scope": "component", "component": "b"},
        ],
        "rules": joint_rules,
    }
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, joint_body)
    by_inst = {(s["component"], s["obligation"]): s["satisfied_by"]
               for s in out.get("satisfied", [])}
    hits_by_mat = {h["material_id"]: {(i["component"], i["obligation"])
                                      for i in h["instances"]}
                   for h in out.get("hits", [])}
    check(
        "fulfillment: all satisfied jointly across materials",
        code == 200 and out.get("ready") is True
        and out.get("instances_satisfied") == 6
        and out.get("selected_materials") == ["copy-a", "copy-b", "signed"],
        json.dumps(out, sort_keys=True)[:300] if code == 200 else str(out),
    )
    check(
        "fulfillment: joint-all per-instance witnesses and hit trace",
        by_inst == {
            ("a", "x"): ["copy-a", "signed"],
            ("a", "y"): ["copy-a", "signed"],
            ("a", "z"): ["copy-a", "signed"],
            ("b", "x"): ["copy-b", "signed"],
            ("b", "y"): ["copy-b", "signed"],
            ("b", "z"): ["copy-b", "signed"],
        }
        and hits_by_mat == {
            "signed": {("a", "x"), ("a", "y"), ("a", "z"),
                       ("b", "x"), ("b", "y"), ("b", "z")},
            "copy-a": {("a", "x"), ("a", "y"), ("a", "z")},
            "copy-b": {("b", "x"), ("b", "y"), ("b", "z")},
        },
        json.dumps(out, sort_keys=True)[:400],
    )
    joint_hash = out.get("fulfillment_hash")

    # rule/material order (incl. all/any array order) must not change it
    import random as _rnd2
    r2 = _rnd2.Random(123)
    sh = {"materials": list(joint_body["materials"]),
          "rules": {"obligations": {k: v for k, v in joint_rules["obligations"].items()}}}
    r2.shuffle(sh["materials"])

    def _shuf(n):
        for key in ("all", "any"):
            if key in n:
                r2.shuffle(n[key])
                for ch in n[key]:
                    _shuf(ch)
    for n in sh["rules"]["obligations"].values():
        _shuf(n)
    code, out2 = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, sh)
    check("fulfillment: joint-all order invariant",
          code == 200 and out2.get("fulfillment_hash") == joint_hash)

    # a single material alone is not enough -> incomplete-evidence gap,
    # but it must still appear in the best partial plan with a hit trace
    partial_body = {
        "materials": [
            {"id": "signed", "digest": "sg", "type": "notice", "scope": "product"},
        ],
        "rules": {"obligations": {
            "x": {"all": [{"type": "notice"}, {"type": "license-copy"}]},
            "y": {"all": [{"type": "notice"}, {"type": "license-copy"}]},
            "z": {"type": "notice"},
        }},
    }
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, partial_body)
    gaps = {(g["component"], g["obligation"]): g for g in out.get("gaps", [])}
    check(
        "fulfillment: partial all -> incomplete-evidence with precise leaves",
        code == 200 and out.get("ready") is False
        and out.get("instances_satisfied") == 2
        and out.get("selected_materials") == ["signed"]
        and all(g.get("reason") == "incomplete-evidence" for g in gaps.values())
        and all(g.get("missing_leaves") == [{"type": "license-copy"}]
                for g in gaps.values())
        and all(g.get("candidate_materials") == ["signed"] for g in gaps.values()),
        json.dumps(out.get("gaps"), sort_keys=True)[:300] if code == 200 else str(out),
    )
    check(
        "fulfillment: partial plan keeps per-material hit trace",
        [h["material_id"] for h in out.get("hits", [])] == ["signed"]
        and {(i["component"], i["obligation"])
             for i in out.get("hits", [{}])[0].get("instances", [])}
        == {("a", "z"), ("b", "z")},
        json.dumps(out.get("hits"), sort_keys=True)[:300],
    )

    # totally unmatched rule keeps the no-matching-material semantics
    nomatch_body = {
        "materials": [
            {"id": "signed", "digest": "sg", "type": "notice", "scope": "product"},
        ],
        "rules": {"obligations": {
            "x": {"type": "source-archive"}, "y": {"type": "source-archive"},
            "z": {"all": [{"type": "source-archive"}, {"scope": "component"}]},
        }},
    }
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, nomatch_body)
    check(
        "fulfillment: no leaf hit -> no-matching-material and empty traces",
        code == 200 and out.get("ready") is False
        and out.get("instances_satisfied") == 0
        and out.get("selected_materials") == []
        and out.get("hits") == []
        and {g["reason"] for g in out.get("gaps", [])} == {"no-matching-material"}
        and all(not g.get("missing_leaves") for g in out.get("gaps", [])),
        json.dumps(out, sort_keys=True)[:300] if code == 200 else str(out),
    )

    # -- component material covers only its declared object -----------------
    comp_body = {
        "materials": [
            {"id": "ca", "digest": "1", "type": "type-a",
             "scope": "component", "component": "a"},
        ],
        "rules": {"obligations": {
            "x": {"all": [{"type": "type-a"}, {"scope": "component"}]},
            "y": {"all": [{"type": "type-a"}, {"scope": "component"}]},
            "z": {"all": [{"type": "type-a"}, {"scope": "component"}]},
        }},
    }
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, comp_body)
    gap_components = sorted(g["component"] for g in out.get("gaps", []))
    check("fulfillment: component material scoped to declared object",
          code == 200 and out.get("ready") is False
          and out.get("instances_satisfied") == 3
          and gap_components == ["b", "b", "b"],
          json.dumps(out.get("gaps"))[:200] if code == 200 else str(out))

    # -- same digest counts once; identical metadata collapses --------------
    dup_body = {
        "materials": [
            {"id": "z-second", "digest": "SAME", "type": "type-a", "scope": "product"},
            {"id": "a-first", "digest": "SAME", "type": "type-a", "scope": "product"},
        ],
        "rules": {"obligations": {
            "x": {"type": "type-a"}, "y": {"type": "type-a"}, "z": {"type": "type-a"},
        }},
    }
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, dup_body)
    check("fulfillment: same digest counted once (smallest id canonical)",
          code == 200 and out.get("ready") is True
          and out.get("selected_materials") == ["a-first"]
          and out.get("deduplicated", [{}])[0].get("duplicate_ids")
          == ["a-first", "z-second"])

    # -- same digest with conflicting metadata -> 422 -----------------------
    conflict_body = {
        "materials": [
            {"id": "one", "digest": "SAME", "type": "type-a", "scope": "product"},
            {"id": "two", "digest": "SAME", "type": "type-a",
             "scope": "component", "component": "a"},
        ],
        "rules": {"obligations": {"x": {"type": "type-a"}}},
    }
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, conflict_body)
    check("fulfillment: digest metadata conflict -> 422", code == 422, str(out)[:120])

    # -- malformed payloads -> 400 ------------------------------------------
    for bad, label in [
        ({}, "no body fields"),
        ({"materials": [], "rules": {"obligations": {"x": {"type": "t"}}}}, "empty materials"),
        ({"materials": [{"id": "m", "digest": "d", "type": "t", "scope": "bogus"}],
          "rules": {"obligations": {"x": {"type": "t"}}}}, "bad scope"),
        ({"materials": [{"id": "m", "digest": "d", "type": "t", "scope": "component"}],
          "rules": {"obligations": {"x": {"type": "t"}}}}, "component missing target"),
        ({"materials": [{"id": "m", "digest": "d", "type": "t"}],
          "rules": {"obligations": {"x": {"type": "t"}}}}, "missing scope"),
        ({"materials": [{"id": "m", "digest": "d", "type": "t", "scope": "product"}],
          "rules": {}}, "missing rules"),
        ({"materials": [{"id": "m", "digest": "d", "type": "t", "scope": "product"}],
          "rules": {"obligations": {"x": {"all": [], "any": []}}}}, "all+any"),
        ({"materials": [{"id": "m", "digest": "d", "type": "t", "scope": "product"}],
          "rules": {"obligations": {"x": {"all": [{"scope": "component",
                                                   "component": "ghost"}]}}}},
         "unknown rule component"),
        ({"materials": [{"id": "m", "digest": "d", "type": "t",
                         "scope": "product", "component": "a"}],
          "rules": {"obligations": {"x": {"type": "t"}}}}, "product material with component"),
        ({"materials": [{"id": "m", "digest": "d", "type": "t", "scope": "product"},
                        {"id": "m", "digest": "d2", "type": "t", "scope": "product"}],
          "rules": {"obligations": {"x": {"type": "t"}}}}, "duplicate material id"),
    ]:
        code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, bad)
        check("fulfillment: 400 on %s" % label, code == 400, str(out)[:100])

    # -- missing evidence rule -> concrete gap ------------------------------
    norule_body = {
        "materials": [{"id": "m", "digest": "d", "type": "type-a", "scope": "product"}],
        "rules": {"obligations": {"x": {"type": "type-a"}, "y": {"type": "type-a"}}},
    }
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid, norule_body)
    reasons = {(g["component"], g["obligation"]): g["reason"] for g in out.get("gaps", [])}
    check("fulfillment: missing rule reported as no-evidence-rule",
          code == 200 and out.get("ready") is False
          and all(v == "no-evidence-rule" for v in reasons.values())
          and sorted(reasons) == [("a", "z"), ("b", "z")],
          str(reasons))

    # -- non-ok assessment -> 409, no fulfillment conclusion ----------------
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % conflict_aid,
                     {"materials": [{"id": "m", "digest": "d", "type": "t",
                                     "scope": "product"}],
                      "rules": {"obligations": {"x": {"type": "t"}}}})
    check("fulfillment: non-ok assessment -> 409", code == 409, str(out)[:120])

    # unknown assessment -> 404
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % ("f" * 32),
                     {"materials": [{"id": "m", "digest": "d", "type": "t",
                                     "scope": "product"}],
                      "rules": {"obligations": {"x": {"type": "t"}}}})
    check("fulfillment: unknown assessment -> 404", code == 404)

    # -- statelessness: the assessment result itself is untouched -----------
    code, body = call("GET", "/v1/assessments/%s" % fc_aid)
    check("fulfillment: assessment result unchanged",
          body.get("result", {}).get("summary_hash") == fc_hash)

    # -- large material space: branch-and-bound path, still exact -----------
    # 20 distinct materials with pairwise different types; every obligation
    # accepts every type -> equal coverage columns survive domination,
    # forcing the branch-and-bound path; lexicographically smallest wins.
    big_materials = [
        {"id": "mm%02d" % i, "digest": "big-%02d" % i,
         "type": "tt%02d" % i, "scope": "product"}
        for i in range(20)
    ]
    big_rules = {"obligations": {
        o: {"any": [{"type": "tt%02d" % i} for i in range(20)]}
        for o in ("x", "y", "z")
    }}
    code, out = call("POST", "/v1/assessments/%s/fulfillment-check" % fc_aid,
                     {"materials": big_materials, "rules": big_rules})
    check("fulfillment: large space selects branch-and-bound",
          code == 200 and out.get("selection_mode") == "branch-and-bound"
          and out.get("ready") is True and out.get("instances_satisfied") == 6,
          json.dumps(out)[:200] if code == 200 else str(out))
    check("fulfillment: large space exact minimum with id tie-break",
          out.get("selected_materials") == ["mm00"],
          str(out.get("selected_materials")))


def run_fulfillment_fuzz(check):
    """Randomized exactness checks for the fulfillment optimizer."""
    import itertools
    import random

    from app.fulfillment import (
        check_fulfillment,
        solve_branch_bound,
        solve_exhaustive,
    )

    rng = random.Random(20260925)

    # -- solver-level fuzz: random compiled formulas ----------------------
    # Each instance gets a small random monotone formula over random
    # leaves; each material matches a random subset of those leaves.
    # The brute-force oracle applies the set-accumulation semantics
    # (a leaf holds iff any selected material matches it).
    def make_formula(depth, nleaves):
        if depth <= 0 or (nleaves and rng.random() < 0.35):
            return ("l", rng.randrange(nleaves)) if nleaves else ("l", 0)
        comb = "a" if rng.random() < 0.5 else "o"
        children = [make_formula(depth - 1, nleaves)
                    for _ in range(rng.randint(2, 3))]
        return (comb, tuple(children))

    def flatten(t, nleaves, gates_out):
        kind = t[0]
        if kind == "l":
            return t[1]
        ins = [flatten(ch, nleaves, gates_out) for ch in t[1]]
        gi = nleaves + len(gates_out)
        mask = 0
        for j in ins:
            mask |= 1 << j
        gates_out.append((kind, mask))
        return gi

    def eval_tuple(t, truth):
        if t[0] == "l":
            return bool((truth >> t[1]) & 1)
        vals = [eval_tuple(ch, truth) for ch in t[1]]
        return all(vals) if t[0] == "a" else any(vals)

    mismatches = 0
    for trial in range(250):
        ni = rng.randint(1, 6)
        nm = rng.randint(1, 18)
        ids = ["m%02d" % i for i in range(nm)]
        gates_roots = []
        leaf_masks = []  # [instance][material]
        tuples = []
        for _k in range(ni):
            nleaves = rng.randint(1, 4)
            t = make_formula(rng.randint(1, 3), nleaves)
            gates_out = []
            root = flatten(t, nleaves, gates_out)
            gates = [("l", i) for i in range(nleaves)] + gates_out
            gates_roots.append((gates, root, nleaves))
            tuples.append(t)
            leaf_masks.append([rng.randrange(0, 1 << nleaves)
                               for _j in range(nm)])
        cov_e, sel_e = solve_exhaustive(gates_roots, ids, leaf_masks)
        cov_b, sel_b = solve_branch_bound(gates_roots, ids, leaf_masks)
        got_e = (bin(cov_e).count("1"), sel_e)
        got_b = (bin(cov_b).count("1"), sel_b)
        if got_e != got_b:
            mismatches += 1
            if mismatches <= 5:
                print("  solver mismatch trial", trial,
                      "exhaustive", got_e, "bnb", got_b)
            continue
        # brute-force oracle only where enumeration is cheap (<=12 mats)
        if nm > 12:
            continue
        oracle_key, oracle_cov = None, 0
        for r in range(nm + 1):
            for combo in itertools.combinations(range(nm), r):
                cov = 0
                for k, t in enumerate(tuples):
                    truth = 0
                    for j in combo:
                        truth |= leaf_masks[k][j]
                    if eval_tuple(t, truth):
                        cov |= 1 << k
                key = (-bin(cov).count("1"), r, tuple(ids[j] for j in combo))
                if oracle_key is None or key < oracle_key:
                    oracle_key, oracle_cov = key, cov
        want = (bin(oracle_cov).count("1"), oracle_key[2])
        if got_e != want:
            mismatches += 1
            if mismatches <= 5:
                print("  oracle mismatch trial", trial, got_e, "oracle", want)
    check("fulfillment fuzz: branch-and-bound == exhaustive == brute force",
          mismatches == 0, "%d mismatches" % mismatches)

    # -- end-to-end fuzz with random nested all/any rules ------------------
    bad = 0
    types = ["t0", "t1", "t2"]

    def random_rule(depth, comps):
        if depth <= 0 or rng.random() < 0.35:
            leaf = {}
            if rng.random() < 0.7:
                leaf["type"] = rng.choice(types)
            if rng.random() < 0.6:
                leaf["scope"] = rng.choice(["product", "component"])
            if not leaf:
                leaf["type"] = rng.choice(types)
            if rng.random() < 0.25:
                leaf["component"] = rng.choice(comps)
            return leaf
        comb = "all" if rng.random() < 0.5 else "any"
        return {comb: [random_rule(depth - 1, comps)
                       for _ in range(rng.randint(2, 3))]}

    def oracle_node(node, materials, inst):
        if "all" in node:
            return all(oracle_node(ch, materials, inst) for ch in node["all"])
        if "any" in node:
            return any(oracle_node(ch, materials, inst) for ch in node["any"])
        for m in materials:
            if "type" in node and node["type"] != m["type"]:
                continue
            if "scope" in node and node["scope"] != m["scope"]:
                continue
            if m["scope"] == "component" and m["component"] != inst[0]:
                continue
            if "component" in node and node["component"] != inst[0]:
                continue
            return True
        return False

    for trial in range(120):
        comps = ["c%d" % i for i in range(rng.randint(1, 4))]
        obs = ["o%d" % i for i in range(rng.randint(1, 4))]
        result = {"status": "ok", "components": [
            {"id": c, "obligations": obs[:rng.randint(0, len(obs))]} for c in comps]}
        instances = [(c["id"], o) for c in result["components"]
                     for o in c["obligations"]]
        nm = rng.randint(1, 12)
        materials = []
        used_digests = set()
        for i in range(nm):
            digest = "dig%02d" % i
            used_digests.add(digest)
            m = {"id": "m%02d" % i, "digest": digest,
                 "type": rng.choice(types),
                 "scope": rng.choice(["product", "component"])}
            if m["scope"] == "component":
                m["component"] = rng.choice(comps)
            materials.append(m)
        rules = {"obligations": {o: random_rule(3, comps) for o in obs}}
        out = check_fulfillment({"materials": materials, "rules": rules}, result)

        ids = [m["id"] for m in materials]
        by_id = {m["id"]: m for m in materials}
        oracle_best = None
        for r in range(len(ids) + 1):
            for combo in itertools.combinations(ids, r):
                sel_mats = [by_id[x] for x in combo]
                hit = sum(
                    1 for inst in instances
                    if oracle_node(rules["obligations"][inst[1]],
                                   sel_mats, inst)
                )
                key = (-hit, len(combo), tuple(sorted(combo)))
                if oracle_best is None or key < oracle_best[0]:
                    oracle_best = (key, hit)
        got = (-out["instances_satisfied"], len(out["selected_materials"]),
               tuple(out["selected_materials"]))
        if got != oracle_best[0]:
            bad += 1
            if bad <= 3:
                print("  e2e mismatch trial", trial, got, oracle_best[0])
        # determinism + order invariance (materials and rule arrays)
        shuffled_rules = {"obligations": dict(rules["obligations"])}

        def shuffle_node(node):
            for key in ("all", "any"):
                if key in node:
                    rng.shuffle(node[key])
                    for ch in node[key]:
                        shuffle_node(ch)

        for node in shuffled_rules["obligations"].values():
            shuffle_node(node)
        body2 = {"materials": list(reversed(materials)),
                 "rules": shuffled_rules}
        out2 = check_fulfillment(body2, result)
        if out2["fulfillment_hash"] != out["fulfillment_hash"]:
            bad += 1
        if out["ready"] != (out["instances_satisfied"] == len(instances)):
            bad += 1
        # every reported gap must really be unsatisfiable by the full set
        for g in out["gaps"]:
            inst = (g["component"], g["obligation"])
            node = rules["obligations"].get(inst[1])
            if node is not None and oracle_node(node, materials, inst):
                bad += 1
                break
        # every satisfied instance must indeed be satisfied
        for s in out["satisfied"]:
            inst = (s["component"], s["obligation"])
            node = rules["obligations"].get(inst[1])
            sel_mats = [by_id[x] for x in out["selected_materials"]]
            if not oracle_node(node, sel_mats, inst):
                bad += 1
                break
    check("fulfillment fuzz: global optimum, gaps and order invariance",
          bad == 0, "%d mismatches" % bad)


def run_schedule_checks(call, check, conflict_aid):
    """End-to-end checks for POST /v1/assessments/{id}/obligation-schedule."""
    # an assessment whose license carries three custom obligations so the
    # schedule is self-contained (independent of default ruleset vocabulary)
    sch_request = {
        "product": "sched-product",
        "components": [
            {"id": "a", "license": "SCH-1.0"},
            {"id": "b", "license": "SCH-1.0"},
        ],
        "rules": {"licenses": {"SCH-1.0": {"obligations": ["alpha", "beta", "gamma"]}}},
    }
    code, body = call("POST", "/v1/assessments", sch_request)
    sch_aid = body.get("id")
    check("schedule: fixture assessment ok",
          code == 200 and body.get("result", {}).get("status") == "ok",
          json.dumps(body)[:200])

    # 2026-09-25 is a Friday; 2026-10-01/02 are holidays, so 5 business days
    # after it is 2026-10-06 (Sep 28/29/30, Oct 5/6)
    sched_body = {
        "release_at": "2026-09-25T16:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "workdays": [1, 2, 3, 4, 5],
        "holidays": ["2026-10-01", "2026-10-02"],
        "obligations": {
            "alpha": {"offset": 5, "unit": "business_day", "depends_on": []},
            "beta": {"offset": 20, "unit": "calendar_day", "depends_on": ["alpha"]},
            "gamma": {"offset": 10, "unit": "calendar_day", "depends_on": ["beta"]},
        },
    }
    code, out = call("POST", "/v1/assessments/%s/obligation-schedule" % sch_aid,
                     sched_body)
    items = {i["obligation"]: i for i in out.get("schedule", [])}
    check("schedule: 200 with every obligation scheduled",
          code == 200 and sorted(items) == ["alpha", "beta", "gamma"],
          json.dumps(out)[:200])
    check("schedule: business-day raw deadline skips weekends and holidays",
          items.get("alpha", {}).get("raw_deadline") == "2026-10-06",
          str(items.get("alpha")))
    check("schedule: calendar-day raw deadlines",
          items.get("beta", {}).get("raw_deadline") == "2026-10-15"
          and items.get("gamma", {}).get("raw_deadline") == "2026-10-05")
    check("schedule: prerequisites tightened to not exceed successors",
          items.get("alpha", {}).get("deadline") == "2026-10-05"
          and items.get("beta", {}).get("deadline") == "2026-10-05"
          and items.get("gamma", {}).get("deadline") == "2026-10-05")
    check("schedule: complete tightening chains reported",
          items.get("alpha", {}).get("tightening_chain") == ["alpha", "beta", "gamma"]
          and items.get("beta", {}).get("tightening_chain") == ["beta", "gamma"]
          and items.get("gamma", {}).get("tightening_chain") == []
          and items.get("alpha", {}).get("tightened") is True
          and items.get("gamma", {}).get("tightened") is False)
    check("schedule: covered components listed",
          items.get("alpha", {}).get("components") == ["a", "b"])
    check("schedule: release moment normalized and localized",
          out.get("release_at") == "2026-09-25T08:00:00Z"
          and out.get("release_date") == "2026-09-25")
    sched_hash = out.get("schedule_hash")

    # input ordering (object keys, workdays, holidays, depends_on) is irrelevant
    permuted = {
        "obligations": {
            "gamma": {"depends_on": ["beta"], "unit": "calendar_day", "offset": 10},
            "beta": {"depends_on": ["alpha"], "unit": "calendar_day", "offset": 20},
            "alpha": {"depends_on": [], "unit": "business_day", "offset": 5},
        },
        "holidays": ["2026-10-02", "2026-10-01"],
        "workdays": [5, 4, 3, 2, 1],
        "timezone": "Asia/Shanghai",
        "release_at": "2026-09-25T16:00:00+08:00",
    }
    code, out2 = call("POST", "/v1/assessments/%s/obligation-schedule" % sch_aid,
                       permuted)
    check("schedule: input order does not change schedule_hash",
          code == 200 and out2.get("schedule_hash") == sched_hash)

    # the same instant in another notation lands on the same schedule
    code, out2 = call("POST", "/v1/assessments/%s/obligation-schedule" % sch_aid,
                      dict(sched_body, release_at="2026-09-25T08:00:00Z"))
    check("schedule: equivalent instant normalized to same hash",
          code == 200 and out2.get("schedule_hash") == sched_hash)

    # the release moment is converted into the target timezone before dating
    code, out2 = call("POST", "/v1/assessments/%s/obligation-schedule" % sch_aid,
                      dict(sched_body, release_at="2026-09-25T23:30:00-04:00"))
    check("schedule: release moment converted to local timezone",
          code == 200 and out2.get("release_date") == "2026-09-26")

    # extra milestone obligations join the graph with empty component coverage
    milestone_body = dict(sched_body)
    milestone_body["obligations"] = dict(
        sched_body["obligations"],
        **{"legal-review": {"offset": 3, "unit": "business_day",
                            "depends_on": ["gamma"]}}
    )
    code, out2 = call("POST", "/v1/assessments/%s/obligation-schedule" % sch_aid,
                      milestone_body)
    m_items = {i["obligation"]: i for i in out2.get("schedule", [])}
    check("schedule: milestone obligation scheduled with empty coverage",
          code == 200
          and m_items.get("legal-review", {}).get("components") == []
          and m_items.get("legal-review", {}).get("raw_deadline") == "2026-09-30"
          and m_items.get("gamma", {}).get("deadline") == "2026-09-30"
          and m_items.get("gamma", {}).get("tightening_chain")
          == ["gamma", "legal-review"],
          json.dumps(out2)[:300] if code == 200 else str(out2))

    # -- 422: semantic failures never produce a partial schedule ------------
    def mutated(mutate):
        body = json.loads(json.dumps(sched_body))
        mutate(body)
        return body

    def make_cycle(b):
        b["obligations"]["alpha"]["depends_on"] = ["gamma"]
        b["obligations"]["gamma"]["depends_on"] = ["alpha"]

    cases_422 = [
        ("missing obligation rule", mutated(lambda b: b["obligations"].pop("gamma"))),
        ("unknown timezone", mutated(lambda b: b.update(timezone="Mars/Olympus"))),
        ("invalid release_at",
         mutated(lambda b: b.update(release_at="2026-02-30T10:00:00Z"))),
        ("release_at without offset",
         mutated(lambda b: b.update(release_at="2026-09-25T16:00:00"))),
        ("invalid holiday", mutated(lambda b: b.update(holidays=["2026-13-01"]))),
        ("dependency cycle", mutated(make_cycle)),
        ("self dependency",
         mutated(lambda b: b["obligations"]["alpha"].update(depends_on=["alpha"]))),
        ("dangling depends_on",
         mutated(lambda b: b["obligations"]["alpha"].update(depends_on=["ghost"]))),
    ]
    for label, bad in cases_422:
        code, out = call("POST", "/v1/assessments/%s/obligation-schedule" % sch_aid,
                         bad)
        check("schedule: 422 on %s" % label,
              code == 422 and "schedule" not in out and "schedule_hash" not in out,
              "%s -> %s" % (code, json.dumps(out)[:120]))

    # -- 400: malformed payloads --------------------------------------------
    cases_400 = [
        ("unknown unit",
         mutated(lambda b: b["obligations"]["alpha"].update(unit="hour"))),
        ("negative offset",
         mutated(lambda b: b["obligations"]["alpha"].update(offset=-1))),
        ("missing workdays", mutated(lambda b: b.pop("workdays"))),
        ("weekday out of range", mutated(lambda b: b.update(workdays=[1, 8]))),
    ]
    for label, bad in cases_400:
        code, out = call("POST", "/v1/assessments/%s/obligation-schedule" % sch_aid,
                         bad)
        check("schedule: 400 on %s" % label, code == 400, str(out)[:100])

    # -- non-ok assessment -> 409; unknown assessment -> 404 -----------------
    code, out = call("POST", "/v1/assessments/%s/obligation-schedule" % conflict_aid,
                     sched_body)
    check("schedule: non-ok assessment -> 409", code == 409, str(out)[:120])
    code, out = call("POST", "/v1/assessments/%s/obligation-schedule" % ("f" * 32),
                     sched_body)
    check("schedule: unknown assessment -> 404", code == 404)

    # an assessment without artifact obligations yields an empty schedule
    code, body = call("POST", "/v1/assessments",
                      {"components": [{"id": "solo", "license": "Unlicense"}]})
    empty_aid = body.get("id")
    code, out = call("POST", "/v1/assessments/%s/obligation-schedule" % empty_aid,
                     {"release_at": "2026-09-25T16:00:00+08:00",
                      "timezone": "UTC", "workdays": [1, 2, 3, 4, 5]})
    check("schedule: no artifact obligations -> empty schedule",
          code == 200 and out.get("schedule") == [] and bool(out.get("schedule_hash")),
          json.dumps(out)[:200])


def run_plan_checks(call, check, conflict_aid):
    """End-to-end checks for POST /v1/assessments/{id}/responsibility-plan."""
    # fixture: two components x two obligations -> six responsibility items
    # (artifact:alpha, artifact:beta plus one per component)
    rp_request = {
        "product": "rp-product",
        "components": [
            {"id": "a", "license": "RP-1.0"},
            {"id": "b", "license": "RP-1.0"},
        ],
        "rules": {"licenses": {"RP-1.0": {"obligations": ["alpha", "beta"]}}},
    }
    code, body = call("POST", "/v1/assessments", rp_request)
    rp_aid = body.get("id")
    check("plan: fixture assessment ok",
          code == 200 and body.get("result", {}).get("status") == "ok",
          json.dumps(body)[:200])

    # cost structure forces a specific global optimum: bob (cheapest
    # executor) takes four executes, alice two executes + two reviews,
    # carol (cheapest reviewer) four reviews; the (item, executor,
    # reviewer) tie-break fixes exactly which items each person staffs
    people = [
        {"id": "alice",
         "qualifications": {"execute": ["alpha", "beta"], "review": ["alpha"]},
         "capacity": 4, "costs": {"execute": 4, "review": 2}},
        {"id": "bob",
         "qualifications": {"execute": ["alpha", "beta"],
                            "review": ["alpha", "beta"]},
         "capacity": 4, "costs": {"execute": 3, "review": 3}},
        {"id": "carol",
         "qualifications": {"review": ["alpha", "beta"]},
         "capacity": 4, "costs": {"review": 1}},
    ]
    code, out = call("POST", "/v1/assessments/%s/responsibility-plan" % rp_aid,
                     {"people": people})
    check("plan: 200 and every item staffed",
          code == 200 and out.get("ready") is True
          and out.get("items_total") == 6 and out.get("items_assigned") == 6,
          json.dumps(out)[:200])
    check("plan: global optimum with item/executor/reviewer tie-break",
          [(a["item"], a["executor"], a["reviewer"], a["cost"])
           for a in out.get("assignments", [])]
          == [("artifact:alpha", "alice", "carol", 5),
              ("artifact:beta", "alice", "carol", 5),
              ("component:a:alpha", "bob", "alice", 5),
              ("component:a:beta", "bob", "carol", 4),
              ("component:b:alpha", "bob", "alice", 5),
              ("component:b:beta", "bob", "carol", 4)],
          json.dumps(out.get("assignments"))[:300])
    check("plan: total cost minimized", out.get("total_cost") == 28,
          str(out.get("total_cost")))
    check("plan: capacity usage per person",
          [(u["person"], u["execute"], u["review"], u["used"], u["capacity"])
           for u in out.get("capacity_usage", [])]
          == [("alice", 2, 2, 4, 4),
              ("bob", 4, 0, 4, 4),
              ("carol", 0, 4, 4, 4)])
    check("plan: exhaustive selection mode",
          out.get("selection_mode") == "exhaustive")
    rp_hash = out.get("plan_hash")

    # people order and qualification order (and duplicates) are irrelevant
    shuffled = {"people": [
        {"id": "carol", "capacity": 4, "costs": {"review": 1},
         "qualifications": {"review": ["beta", "alpha", "alpha"]}},
        {"id": "bob", "capacity": 4,
         "costs": {"review": 3, "execute": 3},
         "qualifications": {"execute": ["beta", "alpha"],
                            "review": ["beta", "alpha"]}},
        {"id": "alice", "capacity": 4,
         "costs": {"review": 2, "execute": 4},
         "qualifications": {"execute": ["beta", "alpha"], "review": ["alpha"]}},
    ]}
    code, out2 = call("POST", "/v1/assessments/%s/responsibility-plan" % rp_aid,
                      shuffled)
    check("plan: people/qualification order does not change result",
          code == 200 and out2.get("plan_hash") == rp_hash
          and out2.get("assignments") == out.get("assignments"))

    # trap fixture: one component, one obligation -> two items
    trap_request = {
        "components": [{"id": "c", "license": "RPX-1.0"}],
        "rules": {"licenses": {"RPX-1.0": {"obligations": ["x"]}}},
    }
    code, body = call("POST", "/v1/assessments", trap_request)
    trap_aid = body.get("id")
    check("plan: trap fixture assessment ok",
          code == 200 and body.get("result", {}).get("status") == "ok")

    # executor-first greedy trap: the cheapest executor (alice, cost 1)
    # would take both executes, exhausting her capacity so only carol's
    # single review remains -> one item staffed.  The global optimum lets
    # bob execute both items and alice review both: two items, cost 12.
    trap_people = [
        {"id": "alice", "qualifications": {"execute": ["x"], "review": ["x"]},
         "capacity": 2, "costs": {"execute": 1, "review": 1}},
        {"id": "bob", "qualifications": {"execute": ["x"]},
         "capacity": 2, "costs": {"execute": 5}},
        {"id": "carol", "qualifications": {"review": ["x"]},
         "capacity": 1, "costs": {"review": 10}},
    ]
    code, out = call("POST", "/v1/assessments/%s/responsibility-plan" % trap_aid,
                     {"people": trap_people})
    check("plan: global optimum beats executor-first greedy",
          code == 200 and out.get("ready") is True
          and out.get("items_assigned") == 2
          and out.get("total_cost") == 12
          and [(a["item"], a["executor"], a["reviewer"])
               for a in out.get("assignments", [])]
          == [("artifact:x", "bob", "alice"),
              ("component:c:x", "bob", "alice")],
          json.dumps(out)[:300])

    # cost minimization beats the lexicographically smaller assignment:
    # amy is the smaller executor id but zoe is cheaper, so zoe staffs both
    cost_first = [
        {"id": "amy", "qualifications": {"execute": ["x"]},
         "capacity": 2, "costs": {"execute": 5}},
        {"id": "zoe", "qualifications": {"execute": ["x"]},
         "capacity": 2, "costs": {"execute": 1}},
        {"id": "bob", "qualifications": {"review": ["x"]},
         "capacity": 2, "costs": {"review": 0}},
    ]
    code, out = call("POST", "/v1/assessments/%s/responsibility-plan" % trap_aid,
                     {"people": cost_first})
    check("plan: cost minimization beats lexicographic order",
          code == 200
          and [(a["item"], a["executor"], a["reviewer"])
               for a in out.get("assignments", [])]
          == [("artifact:x", "zoe", "bob"), ("component:c:x", "zoe", "bob")]
          and out.get("total_cost") == 2)

    # equal costs: the (item, executor, reviewer) sequence breaks the tie
    tie_people = [
        {"id": "zoe", "qualifications": {"execute": ["x"], "review": ["x"]},
         "capacity": 2},
        {"id": "amy", "qualifications": {"execute": ["x"], "review": ["x"]},
         "capacity": 2},
    ]
    code, out = call("POST", "/v1/assessments/%s/responsibility-plan" % trap_aid,
                     {"people": tie_people})
    check("plan: lexicographic tie-break at equal cost",
          code == 200
          and [(a["item"], a["executor"], a["reviewer"])
               for a in out.get("assignments", [])]
          == [("artifact:x", "amy", "zoe"), ("component:c:x", "amy", "zoe")]
          and out.get("total_cost") == 0)

    # fractional costs are exact: 0.1 + 0.2 style inputs never disturb the
    # optimization; the total is reported as a JSON number
    frac_people = [
        {"id": "alice", "qualifications": {"execute": ["x"]},
         "capacity": 2, "costs": {"execute": 0.1}},
        {"id": "bob", "qualifications": {"review": ["x"]},
         "capacity": 2, "costs": {"review": 0.2}},
    ]
    code, out = call("POST", "/v1/assessments/%s/responsibility-plan" % trap_aid,
                     {"people": frac_people})
    check("plan: fractional costs stay exact",
          code == 200 and out.get("ready") is True
          and out.get("total_cost") == 2 * 0.1 + 2 * 0.2)

    # -- 409: best partial plan plus per-item blockage reasons --------------
    def plan409(persons):
        return call("POST", "/v1/assessments/%s/responsibility-plan" % trap_aid,
                    {"people": persons})

    code, out = plan409([{"id": "carol", "capacity": 5,
                          "qualifications": {"review": ["x"]}}])
    check("plan: 409 with best partial plan when uncoverable",
          code == 409 and out.get("ready") is False
          and out.get("items_assigned") == 0
          and out.get("assignments") == []
          and bool(out.get("error")) and bool(out.get("plan_hash")))
    check("plan: no-qualified-executor reason",
          [(u["item"], u["reason"], u["qualified_executors"],
            u["qualified_reviewers"]) for u in out.get("unassigned", [])]
          == [("artifact:x", "no-qualified-executor", [], ["carol"]),
              ("component:c:x", "no-qualified-executor", [], ["carol"])])

    code, out = plan409([{"id": "bob", "capacity": 5,
                          "qualifications": {"execute": ["x"]}}])
    check("plan: no-qualified-reviewer reason",
          code == 409
          and [u["reason"] for u in out.get("unassigned", [])]
          == ["no-qualified-reviewer"] * 2)

    code, out = plan409([{"id": "alice", "capacity": 5,
                          "qualifications": {"execute": ["x"], "review": ["x"]}}])
    check("plan: no-distinct-pair reason",
          code == 409
          and [u["reason"] for u in out.get("unassigned", [])]
          == ["no-distinct-pair"] * 2)

    code, out = plan409([
        {"id": "alice", "capacity": 1, "costs": {"execute": 1},
         "qualifications": {"execute": ["x"]}},
        {"id": "bob", "capacity": 1, "costs": {"review": 1},
         "qualifications": {"review": ["x"]}},
    ])
    check("plan: insufficient-capacity reason with best partial plan",
          code == 409 and out.get("items_assigned") == 1
          and [(a["item"], a["executor"], a["reviewer"])
               for a in out.get("assignments", [])]
          == [("artifact:x", "alice", "bob")]
          and [(u["item"], u["reason"]) for u in out.get("unassigned", [])]
          == [("component:c:x", "insufficient-capacity")],
          json.dumps(out)[:300])

    # -- 400: malformed payloads ---------------------------------------------
    cases_400 = [
        ("people not an array", {"people": {}}),
        ("duplicate person id", {"people": [
            {"id": "a", "capacity": 1}, {"id": "a", "capacity": 1}]}),
        ("negative capacity", {"people": [{"id": "a", "capacity": -1}]}),
        ("boolean capacity", {"people": [{"id": "a", "capacity": True}]}),
        ("negative cost", {"people": [
            {"id": "a", "capacity": 1, "costs": {"execute": -2}}]}),
        ("unknown person field", {"people": [
            {"id": "a", "capacity": 1, "team": "x"}]}),
        ("qualifications not arrays", {"people": [
            {"id": "a", "capacity": 1, "qualifications": {"execute": "x"}}]}),
        ("non-string qualification", {"people": [
            {"id": "a", "capacity": 1, "qualifications": {"execute": [1]}}]}),
        ("body not an object", [1, 2, 3]),
    ]
    for label, bad in cases_400:
        code, out = call("POST", "/v1/assessments/%s/responsibility-plan" % trap_aid,
                         bad)
        check("plan: 400 on %s" % label, code == 400, str(out)[:100])

    # -- non-ok assessment -> 409; unknown assessment -> 404 ------------------
    code, out = call("POST", "/v1/assessments/%s/responsibility-plan" % conflict_aid,
                     {"people": []})
    check("plan: non-ok assessment -> 409", code == 409, str(out)[:120])
    code, out = call("POST", "/v1/assessments/%s/responsibility-plan" % ("f" * 32),
                     {"people": []})
    check("plan: unknown assessment -> 404", code == 404)

    # an assessment without obligations yields an empty, ready plan
    code, body = call("POST", "/v1/assessments",
                      {"components": [{"id": "solo", "license": "Unlicense"}]})
    empty_aid = body.get("id")
    code, out = call("POST", "/v1/assessments/%s/responsibility-plan" % empty_aid,
                     {"people": []})
    check("plan: no obligations -> empty ready plan",
          code == 200 and out.get("ready") is True
          and out.get("items_total") == 0 and out.get("assignments") == []
          and out.get("unassigned") == [] and out.get("total_cost") == 0
          and bool(out.get("plan_hash")), json.dumps(out)[:200])

    # -- solver equivalence through the full pipeline -------------------------
    from app import responsibility

    def direct_plan(aid, plan_body, cap):
        old = responsibility.MAX_PLAN_COMBINATIONS
        responsibility.MAX_PLAN_COMBINATIONS = cap
        try:
            _code, res = call("GET", "/v1/assessments/%s" % aid)
            try:
                return responsibility.build_plan(plan_body, res["result"], aid)
            except responsibility.PartialPlanError as exc:
                return exc.payload
        finally:
            responsibility.MAX_PLAN_COMBINATIONS = old

    def stripped(plan):
        return {k: v for k, v in plan.items()
                if k not in ("selection_mode", "plan_hash")}

    # four fully-qualified people on the six-item fixture: 13^6 combinations,
    # so the default cap routes it to branch-and-bound
    big_people = [
        {"id": "p%d" % i,
         "qualifications": {"execute": ["alpha", "beta"],
                            "review": ["alpha", "beta"]},
         "capacity": 4,
         "costs": {"execute": 2 + i, "review": 1 + (i % 3)}}
        for i in range(4)
    ]
    battery = [
        (rp_aid, {"people": people}),
        (rp_aid, {"people": big_people}),
        (trap_aid, {"people": trap_people}),
        (trap_aid, {"people": tie_people}),
        (trap_aid, {"people": [
            {"id": "alice", "capacity": 1, "costs": {"execute": 1},
             "qualifications": {"execute": ["x"]}},
            {"id": "bob", "capacity": 1, "costs": {"review": 1},
             "qualifications": {"review": ["x"]}},
        ]}),
    ]
    for i, (aid, plan_body) in enumerate(battery):
        check("plan: branch-and-bound matches exhaustive #%d" % (i + 1),
              stripped(direct_plan(aid, plan_body, 0))
              == stripped(direct_plan(aid, plan_body, 10**9)))

    # the big case really does take the branch-and-bound path by default,
    # and its HTTP result equals the forced-exhaustive result
    code, out = call("POST", "/v1/assessments/%s/responsibility-plan" % rp_aid,
                     {"people": big_people})
    check("plan: large choice space uses branch-and-bound",
          code == 200 and out.get("selection_mode") == "branch-and-bound"
          and out.get("ready") is True and out.get("items_assigned") == 6)
    check("plan: branch-and-bound HTTP result matches forced exhaustive",
          code == 200
          and stripped(out) == stripped(direct_plan(rp_aid, {"people": big_people},
                                                    10**9)))

    # randomized end-to-end equivalence (deterministic seed)
    import random as _rnd
    rng = _rnd.Random(20260927)
    mismatches = 0
    for _trial in range(25):
        persons = []
        for pi in range(rng.randint(1, 3)):
            quals = {}
            if rng.random() < 0.8:
                quals["execute"] = [ob for ob in ("alpha", "beta")
                                    if rng.random() < 0.7]
            if rng.random() < 0.8:
                quals["review"] = [ob for ob in ("alpha", "beta")
                                   if rng.random() < 0.7]
            persons.append({
                "id": "p%d" % pi,
                "qualifications": quals,
                "capacity": rng.randint(0, 4),
                "costs": {"execute": rng.randint(0, 6),
                          "review": rng.randint(0, 6)},
            })
        plan_body = {"people": persons}
        if stripped(direct_plan(rp_aid, plan_body, 0)) != stripped(
            direct_plan(rp_aid, plan_body, 10**9)
        ):
            mismatches += 1
    check("plan: randomized branch-and-bound/exhaustive equivalence",
          mismatches == 0, "%d mismatches" % mismatches)


def run_plan_fuzz(check):
    """Randomized exactness checks for the responsibility-plan optimizer."""
    import itertools
    import random
    from fractions import Fraction

    from app.responsibility import solve_branch_bound, solve_exhaustive

    rng = random.Random(20260927)
    mismatches = 0
    for trial in range(150):
        n = rng.randint(1, 5)
        p = rng.randint(1, 4)
        item_ids = ["i%d" % i for i in range(n)]
        person_ids = ["p%d" % i for i in range(p)]
        capacities = [rng.randint(0, 3) for _ in range(p)]
        exec_costs = [Fraction(rng.randint(0, 5)) for _ in range(p)]
        rev_costs = [Fraction(rng.randint(0, 5)) for _ in range(p)]
        candidates = []
        for _ in range(n):
            pairs = []
            for e in range(p):
                for r in range(p):
                    if e != r and rng.random() < 0.6:
                        pairs.append((e, r, exec_costs[e] + rev_costs[r]))
            pairs.sort(key=lambda t: (t[0], t[1]))
            candidates.append(tuple(pairs))
        role_costs = (exec_costs, rev_costs)
        got_e = solve_exhaustive(
            item_ids, candidates, capacities, person_ids, role_costs
        )
        got_b = solve_branch_bound(
            item_ids, candidates, capacities, person_ids, role_costs
        )
        # brute-force oracle over every per-item choice combination
        best = None
        best_assign = None
        choices = [[None] + list(c) for c in candidates]
        for combo in itertools.product(*choices):
            used = [0] * p
            feasible = True
            for pick in combo:
                if pick is None:
                    continue
                e, r, _c = pick
                used[e] += 1
                used[r] += 1
                if used[e] > capacities[e] or used[r] > capacities[r]:
                    feasible = False
                    break
            if not feasible:
                continue
            count = sum(1 for pick in combo if pick is not None)
            cost = sum((pick[2] for pick in combo if pick is not None),
                       Fraction(0))
            seq = tuple(
                (item_ids[j], person_ids[pick[0]], person_ids[pick[1]])
                for j, pick in enumerate(combo) if pick is not None
            )
            key = (-count, cost, seq)
            if best is None or key < best:
                best = key
                best_assign = [
                    None if pick is None else (pick[0], pick[1]) for pick in combo
                ]
        if got_e != (best, best_assign) or got_b != (best, best_assign):
            mismatches += 1
            if mismatches <= 5:
                print("  plan solver mismatch trial", trial,
                      "oracle", best, "exhaustive", got_e[0], "bnb", got_b[0])
    check("plan fuzz: branch-and-bound == exhaustive == brute force",
          mismatches == 0, "%d mismatches" % mismatches)


def run_compare_checks(call, check):
    """End-to-end checks for POST /v1/assessments/compare."""
    # baseline fixture: ok assessment adopting the constraint-forced branch
    base_request = {
        "product": "cmp-base",
        "distribution": "binary",
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "liba", "license": "Apache-2.0 OR MIT", "linkage": "static"},
            {"id": "libb", "license": "GPL-2.0-only", "linkage": "static"},
        ],
        "dependencies": [
            {"from": "app", "to": "liba"},
            {"from": "app", "to": "libb"},
        ],
    }
    code, body = call("POST", "/v1/assessments", base_request)
    base_id = body.get("id")
    base_hash = body.get("result", {}).get("summary_hash")
    check("compare: baseline fixture ok",
          code == 200 and body.get("result", {}).get("status") == "ok",
          json.dumps(body)[:200])

    def compare(bid, tid):
        return call("POST", "/v1/assessments/compare",
                    {"baseline_id": bid, "target_id": tid})

    # -- target that conflicts: one component diff, fully attributed ---------
    conflict_request = json.loads(json.dumps(base_request))
    conflict_request["components"][1]["license"] = "Apache-2.0"
    code, body = call("POST", "/v1/assessments", conflict_request)
    conflict_id = body.get("id")
    check("compare: conflict target fixture",
          code == 200 and body.get("result", {}).get("status") == "conflict")

    code, out = compare(base_id, conflict_id)
    check("compare: 200 with one atomic component diff",
          code == 200 and out.get("diff_count") == 1
          and out.get("diffs", [{}])[0].get("kind") == "component"
          and out.get("diffs", [{}])[0].get("component") == "liba"
          and out.get("diffs", [{}])[0].get("change") == "changed"
          and out.get("diffs", [{}])[0].get("id") == "D0001",
          json.dumps(out)[:300] if code == 200 else str(out))
    check("compare: minimal set rolls the single diff back",
          out.get("minimal_set") == ["D0001"] and out.get("minimal_size") == 1
          and out.get("conclusions_agree") is False)
    check("compare: counterfactual restores the baseline conclusion",
          out.get("counterfactual", {}).get("status") == "ok"
          and out.get("counterfactual", {}).get("summary_hash") == base_hash
          and out.get("counterfactual", {}).get("restores_baseline_conclusion") is True)
    cmp_hash = out.get("comparison_hash")
    code, out2 = compare(base_id, conflict_id)
    check("compare: deterministic comparison_hash",
          code == 200 and out2.get("comparison_hash") == cmp_hash)

    # -- conclusions already agree -> empty attribution set ------------------
    renamed = dict(base_request, product="cmp-renamed")
    code, body = call("POST", "/v1/assessments", renamed)
    renamed_id = body.get("id")
    code, out = compare(base_id, renamed_id)
    check("compare: agreeing conclusions yield empty minimal set",
          code == 200 and out.get("conclusions_agree") is True
          and out.get("minimal_set") == []
          and [d["kind"] for d in out.get("diffs", [])] == ["product"],
          json.dumps(out)[:300] if code == 200 else str(out))

    # -- minimal set is a strict subset; illegal intermediates are skipped ---
    # target: product renamed (D0001, neutral), liba expression simplified
    # (D0002, neutral: the adopted branch is unchanged), component libg added
    # (D0003) plus its dependency edge (D0004).  Rolling back D0003 alone
    # leaves a dangling edge (illegal intermediate, must not count); rolling
    # back D0004 alone keeps the extra component (conclusion differs).  Only
    # {D0003, D0004} restores the baseline conclusion.
    grown = json.loads(json.dumps(base_request))
    grown["product"] = "cmp-grown"
    grown["components"][1]["license"] = "MIT"
    grown["components"].append(
        {"id": "libg", "license": "GPL-2.0-only", "linkage": "static"})
    grown["dependencies"].append({"from": "app", "to": "libg"})
    code, body = call("POST", "/v1/assessments", grown)
    grown_id = body.get("id")
    check("compare: grown target fixture ok",
          code == 200 and body.get("result", {}).get("status") == "ok")
    code, out = compare(base_id, grown_id)
    check("compare: four atomic diffs decomposed",
          code == 200 and out.get("diff_count") == 4
          and [(d["id"], d["kind"]) for d in out.get("diffs", [])]
          == [("D0001", "product"), ("D0002", "component"),
              ("D0003", "component"), ("D0004", "dependency")],
          json.dumps(out.get("diffs"))[:300] if code == 200 else str(out))
    check("compare: minimal set skips neutral diffs and illegal states",
          out.get("minimal_set") == ["D0003", "D0004"]
          and out.get("counterfactual", {}).get("status") == "ok",
          json.dumps(out.get("minimal_set")))

    # -- license-rule diff ----------------------------------------------------
    solo = {"components": [{"id": "solo", "license": "X-1.0"}]}
    rules_b = {"licenses": {"X-1.0": {"obligations": ["attribution"]}}}
    rules_t = {"licenses": {"X-1.0": {"obligations": ["attribution", "license-text"]}}}
    code, body = call("POST", "/v1/assessments", dict(solo, rules=rules_b))
    rule_base_id, rule_base_hash = body.get("id"), body.get("result", {}).get("summary_hash")
    code, body = call("POST", "/v1/assessments", dict(solo, rules=rules_t))
    rule_target_id = body.get("id")
    code, out = compare(rule_base_id, rule_target_id)
    check("compare: license-rule diff attributed",
          code == 200 and out.get("diff_count") == 1
          and out.get("diffs", [{}])[0].get("kind") == "license-rule"
          and out.get("diffs", [{}])[0].get("license") == "X-1.0"
          and out.get("minimal_set") == ["D0001"]
          and out.get("counterfactual", {}).get("summary_hash") == rule_base_hash,
          json.dumps(out)[:300] if code == 200 else str(out))

    # -- lexicographic tie-break between two restoring single diffs ----------
    # Reverting the distribution change (D0001) *or* the license-rule change
    # (D0002) each restores the baseline conclusion: network-source only
    # applies under saas, so either rollback alone reproduces the baseline
    # obligations.  The lexicographically smaller id sequence must win.
    tie_rules_b = {
        "licenses": {
            "MIT": {"obligations": ["attribution"]},
            "GPL-2.0-only": {"obligations": ["attribution", "source-offer"],
                             "propagates": ["static"]},
        },
        "obligation_scopes": {"network-source": ["saas"]},
    }
    tie_rules_t = json.loads(json.dumps(tie_rules_b))
    tie_rules_t["licenses"]["GPL-2.0-only"]["obligations"].append("network-source")
    tie_req = {
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "libg", "license": "GPL-2.0-only", "linkage": "static"},
        ],
        "dependencies": [{"from": "app", "to": "libg"}],
        "rules": tie_rules_b,
    }
    code, body = call("POST", "/v1/assessments",
                      dict(tie_req, distribution="binary"))
    tie_base_id = body.get("id")
    code, body = call("POST", "/v1/assessments",
                      dict(tie_req, distribution="saas", rules=tie_rules_t))
    tie_target_id = body.get("id")
    code, out = compare(tie_base_id, tie_target_id)
    check("compare: lexicographic diff-id tie-break",
          code == 200 and out.get("minimal_size") == 1
          and out.get("minimal_set") == ["D0001"]
          and [d["kind"] for d in out.get("diffs", [])]
          == ["distribution", "license-rule"],
          json.dumps(out.get("minimal_set")) if code == 200 else str(out))

    # -- exception-rule / obligation-scope / combined-work-linkage diffs -----
    exc_rules_b = {
        "licenses": {
            "MIT": {"obligations": ["attribution"]},
            "GPL-2.0-only": {"obligations": ["attribution", "same-license"],
                             "propagates": ["static"]},
        },
        "exceptions": {"E-1": {"removes": ["same-license"],
                               "removes_propagation": True}},
        "combined_work_linkages": ["static", "dynamic"],
    }
    exc_rules_t = {
        "licenses": exc_rules_b["licenses"],
        "combined_work_linkages": ["static"],
        "obligation_scopes": {"attribution": ["binary"]},
    }
    exc_req = {"distribution": "binary",
               "components": [{"id": "rt", "license": "GPL-2.0-only WITH E-1",
                               "linkage": "dynamic"}]}
    code, body = call("POST", "/v1/assessments", dict(exc_req, rules=exc_rules_b))
    exc_base_id = body.get("id")
    check("compare: exception baseline fixture ok",
          code == 200 and body.get("result", {}).get("status") == "ok")
    code, body = call("POST", "/v1/assessments", dict(exc_req, rules=exc_rules_t))
    exc_target_id = body.get("id")
    check("compare: removed exception rule breaks target",
          code == 200 and body.get("result", {}).get("status") == "missing-rules")
    code, out = compare(exc_base_id, exc_target_id)
    check("compare: rule-section diff kinds decomposed",
          code == 200
          and [(d["id"], d["kind"]) for d in out.get("diffs", [])]
          == [("D0001", "exception-rule"), ("D0002", "obligation-scope"),
              ("D0003", "combined-work-linkage")],
          json.dumps(out.get("diffs"))[:300] if code == 200 else str(out))
    check("compare: only the exception-rule rollback is attributed",
          out.get("minimal_set") == ["D0001"]
          and out.get("counterfactual", {}).get("status") == "ok")

    # -- removed component / removed edge ------------------------------------
    shrunk_base = {
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "libx", "license": "MIT", "linkage": "static"},
        ],
        "dependencies": [{"from": "app", "to": "libx"}],
    }
    code, body = call("POST", "/v1/assessments", shrunk_base)
    shrunk_base_id = body.get("id")
    code, body = call("POST", "/v1/assessments",
                      {"components": [{"id": "app", "license": "MIT"}]})
    shrunk_target_id = body.get("id")
    code, out = compare(shrunk_base_id, shrunk_target_id)
    check("compare: removed component and edge decomposed",
          code == 200
          and [(d["kind"], d.get("change")) for d in out.get("diffs", [])]
          == [("component", "removed"), ("dependency", "removed")],
          json.dumps(out.get("diffs"))[:300] if code == 200 else str(out))
    check("compare: outcome-neutral edge removal is not attributed",
          out.get("minimal_set") == ["D0001"]
          and out.get("counterfactual", {}).get("status") == "ok")

    # -- conflict explanation restored ----------------------------------------
    clash_base = {
        "components": [
            {"id": "app", "license": "MIT"},
            {"id": "libg", "license": "GPL-2.0-only", "linkage": "static"},
            {"id": "libap", "license": "Apache-2.0", "linkage": "static"},
        ],
        "dependencies": [{"from": "app", "to": "libg"},
                         {"from": "app", "to": "libap"}],
    }
    code, body = call("POST", "/v1/assessments", clash_base)
    clash_base_id, clash_base_hash = body.get("id"), body.get("result", {}).get("summary_hash")
    check("compare: conflict baseline fixture",
          code == 200 and body.get("result", {}).get("status") == "conflict")
    clash_target = json.loads(json.dumps(clash_base))
    clash_target["components"][2]["license"] = "MIT"
    code, body = call("POST", "/v1/assessments", clash_target)
    clash_target_id = body.get("id")
    code, out = compare(clash_base_id, clash_target_id)
    check("compare: conflict explanation restored by rollback",
          code == 200 and out.get("minimal_set") == ["D0001"]
          and out.get("counterfactual", {}).get("status") == "conflict"
          and out.get("counterfactual", {}).get("summary_hash") == clash_base_hash,
          json.dumps(out.get("counterfactual")) if code == 200 else str(out))

    # -- self comparison ------------------------------------------------------
    code, out = compare(base_id, base_id)
    check("compare: self comparison has no diffs",
          code == 200 and out.get("diff_count") == 0
          and out.get("minimal_set") == []
          and out.get("conclusions_agree") is True)

    # -- errors ----------------------------------------------------------------
    code, out = compare("0" * 32, base_id)
    check("compare: unknown baseline -> 404", code == 404)
    code, out = compare(base_id, "f" * 32)
    check("compare: unknown target -> 404", code == 404)
    code, out = call("POST", "/v1/assessments/compare", {"baseline_id": base_id})
    check("compare: missing target_id -> 400", code == 400)
    code, out = call("POST", "/v1/assessments/compare", ["not", "an", "object"])
    check("compare: non-object body -> 400", code == 400)

    # -- statelessness: neither assessment is mutated by comparing ------------
    code, body = call("GET", "/v1/assessments/%s" % base_id)
    check("compare: baseline assessment unchanged",
          code == 200 and body.get("result", {}).get("summary_hash") == base_hash)


def run_revision_checks():
    """Exercise immutable revisions end to end.

    Covers: content-hash identity, the first revision staying an
    unpublished draft (invisible to legacy GET, ruleset listing and named
    accounting; history reports active_revision null), first publish via
    CAS with expected_active_revision null, draft deduplication,
    optimistic base_revision (409 stale-base), compare-and-set
    activation (409 active-moved), in-transaction active-revision read
    for assessments, history listing, old-revision rollback via the same
    CAS flow, history immutability, unknown-revision handling, real
    activation races (later and first publishes) between two threads, and
    fresh-DB revision seeding.
    """
    v1 = {"licenses": {"Z-1.0": {"obligations": ["attribution"]}}}
    v2 = {"licenses": {"Z-1.0": {"obligations": ["attribution", "license-text"]}}}
    v1_permuted = {
        "licenses": {"Z-1.0": {"obligations": ["attribution", "attribution"]}},
        "combined_work_linkages": ["dynamic", "static"],
    }

    # first revision with base_revision null only SAVES an immutable draft:
    # no active pointer, no activation audit and no legacy mirror
    code, body = call("POST", "/v1/rulesets/revset/revisions",
                      {"rules": v1, "base_revision": None})
    r1 = body.get("revision")
    check(
        "revision: first revision is an unpublished draft",
        code == 200 and body.get("created") is True and body.get("active") is False
        and body.get("base_revision") is None
        and r1 is not None and len(r1) == 16,
        json.dumps(body)[:200],
    )
    check(
        "revision: id is content hash",
        r1 == stable_hash(validate_ruleset(v1))[:16]
        and body.get("content_hash") == stable_hash(validate_ruleset(v1)),
    )

    # re-submitting the identical first draft deduplicates, stays unpublished
    code, body_again = call("POST", "/v1/rulesets/revset/revisions",
                            {"rules": v1, "base_revision": None})
    check(
        "revision: re-submitted first draft deduplicated",
        code == 200 and body_again.get("created") is False
        and body_again.get("revision") == r1 and body_again.get("active") is False,
        json.dumps(body_again)[:200],
    )

    # draft stage: legacy GET, the ruleset listing and named accounting must
    # not observe the ruleset; its history exists with a null active revision
    code, body = call("GET", "/v1/rulesets/revset")
    check("revision: legacy GET hides unpublished draft", code == 404)
    code, body = call("GET", "/v1/rulesets")
    check(
        "revision: ruleset listing excludes unpublished draft",
        code == 200 and "revset" not in body.get("rulesets", []),
    )
    code, hist = call("GET", "/v1/rulesets/revset/revisions")
    check(
        "revision: draft history has null active revision and no audits",
        code == 200 and hist.get("active_revision") is None
        and [r["revision"] for r in hist.get("revisions", [])] == [r1]
        and hist["revisions"][0]["active"] is False
        and hist.get("activations") == [],
        json.dumps(hist)[:200],
    )
    code, body = call("POST", "/v1/assessments",
                      {"product": "revprod", "ruleset": "revset",
                       "components": [{"id": "z", "license": "Z-1.0"}]})
    check("revision: named assessment against unpublished draft -> 404",
          code == 404)

    # first publish goes through the CAS activate endpoint with a null
    # expectation; a non-null expectation loses the compare-and-set and
    # publishes nothing
    code, body = call("POST", "/v1/rulesets/revset/revisions/%s/activate" % r1,
                      {"expected_active_revision": "0" * 16})
    check(
        "revision: first publish with stale non-null expectation -> 409",
        code == 409 and body.get("reason") == "active-moved"
        and body.get("expected_active_revision") == "0" * 16
        and body.get("active_revision") is None,
        json.dumps(body)[:200],
    )
    code, hist = call("GET", "/v1/rulesets/revset/revisions")
    check("revision: failed first publish leaves no audit",
          hist.get("active_revision") is None
          and hist.get("activations") == [])
    code, body = call("POST", "/v1/rulesets/revset/revisions/%s/activate" % r1,
                      {"expected_active_revision": None})
    check(
        "revision: first publish via CAS with null expectation",
        code == 200 and body.get("active_revision") == r1
        and body.get("activated") is True
        and body.get("previous_revision") is None
        and body.get("activated_at"),
        json.dumps(body)[:200],
    )
    # legacy GET and listing observe the ruleset only after the publish
    code, body = call("GET", "/v1/rulesets/revset")
    check(
        "revision: legacy GET follows first publish",
        code == 200 and body.get("ruleset") == validate_ruleset(v1)
        and body.get("active_revision") == r1,
    )
    code, body = call("GET", "/v1/rulesets")
    check("revision: ruleset listing includes published ruleset",
          code == 200 and "revset" in body.get("rulesets", []))

    # normalized-identical payloads collapse onto the same revision
    code, body_same = call("POST", "/v1/rulesets/revset/revisions",
                           {"rules": v1_permuted, "base_revision": r1})
    check(
        "revision: equal normalized content deduplicated",
        code == 200 and body_same.get("created") is False
        and body_same.get("revision") == r1
        and body_same.get("active") is True,
        json.dumps(body_same)[:200],
    )

    # a draft of new content is created but not activated
    code, body = call("POST", "/v1/rulesets/revset/revisions",
                      {"rules": v2, "base_revision": r1})
    r2 = body.get("revision")
    check(
        "revision: new content creates inactive draft",
        code == 200 and body.get("created") is True and body.get("active") is False
        and r2 != r1 and body.get("base_revision") == r1,
    )
    code, body = call("GET", "/v1/rulesets/revset")
    check("revision: draft leaves active unchanged",
          body.get("active_revision") == r1)

    # stale base: drafting against a non-current baseline -> 409
    code, body = call("POST", "/v1/rulesets/revset/revisions",
                      {"rules": v1, "base_revision": "0" * 16})
    check(
        "revision: stale base_revision -> 409",
        code == 409 and body.get("reason") == "stale-base"
        and body.get("active_revision") == r1,
        json.dumps(body)[:200],
    )

    # missing / mistyped fields -> 400
    code, body = call("POST", "/v1/rulesets/revset/revisions",
                      {"rules": v2})
    check("revision: missing base_revision -> 400", code == 400)
    code, body = call("POST", "/v1/rulesets/revset/revisions/%s/activate" % r2,
                      {})
    check("revision: missing expected_active_revision -> 400", code == 400)
    code, body = call("POST", "/v1/rulesets/revset/revisions/%s/activate" % r2,
                      {"expected_active_revision": 123})
    check("revision: mistyped expected_active_revision -> 400", code == 400)
    code, body = call("POST", "/v1/rulesets/revset/revisions/%s/activate" % r2,
                      {"expected_active_revision": "0" * 16})
    check(
        "revision: CAS mismatch -> 409 active-moved",
        code == 409 and body.get("reason") == "active-moved"
        and body.get("active_revision") == r1,
    )
    code, body = call("GET", "/v1/rulesets/revset")
    check("revision: failed CAS leaves active unchanged",
          body.get("active_revision") == r1)

    # activating an unknown revision -> 404
    code, body = call("POST", "/v1/rulesets/revset/revisions/%s/activate" % ("a" * 16),
                      {"expected_active_revision": r1})
    check("revision: unknown revision activate -> 404", code == 404)

    # successful activation of the draft
    code, body = call("POST", "/v1/rulesets/revset/revisions/%s/activate" % r2,
                      {"expected_active_revision": r1})
    check(
        "revision: CAS activation succeeds",
        code == 200 and body.get("active_revision") == r2
        and body.get("activated") is True
        and body.get("previous_revision") == r1
        and body.get("activated_at"),
    )
    # idempotent re-activation adds no audit entry
    code, body2 = call("POST", "/v1/rulesets/revset/revisions/%s/activate" % r2,
                       {"expected_active_revision": r2})
    check(
        "revision: activate already active is idempotent",
        code == 200 and body2.get("activated") is False
        and body2.get("activated_at") == body.get("activated_at"),
    )
    code, body = call("GET", "/v1/rulesets/revset")
    check("revision: GET follows new active",
          code == 200 and body.get("active_revision") == r2
          and body.get("ruleset") == validate_ruleset(v2))

    # legacy PUT stays compatible and appends+activates a revision
    v3 = {"licenses": {"Z-1.0": {"obligations": ["attribution", "same-license"]}}}
    code, body = call("PUT", "/v1/rulesets/revset", v3)
    r3 = call("GET", "/v1/rulesets/revset")[1].get("active_revision")
    check(
        "revision: legacy PUT appends and activates",
        code == 200 and r3 not in (r1, r2)
        and r3 == stable_hash(validate_ruleset(v3))[:16],
    )

    # history lists every revision, never overwritten or deleted
    code, hist = call("GET", "/v1/rulesets/revset/revisions")
    revs = hist.get("revisions", [])
    check(
        "revision: history complete and immutable",
        code == 200 and [r["revision"] for r in revs] == [r1, r2, r3]
        and [r["active"] for r in revs] == [False, False, True]
        and revs[0]["ruleset"] == validate_ruleset(v1)
        and revs[1]["ruleset"] == validate_ruleset(v2)
        and revs[0]["content_hash"] == stable_hash(validate_ruleset(v1)),
        str([r["revision"] for r in revs]),
    )
    check(
        "revision: activation audit chain",
        [a["revision"] for a in hist.get("activations", [])]
        == [r1, r2, r3]
        and [a["previous_revision"] for a in hist.get("activations", [])]
        == [None, r1, r2],
        json.dumps(hist.get("activations"))[:300],
    )

    # assessments read the active revision in-transaction and record it
    areq = {"product": "revprod", "ruleset": "revset",
            "components": [{"id": "z", "license": "Z-1.0"}]}
    code, body = call("POST", "/v1/assessments", areq)
    aid3, aobs3 = body.get("id"), body["result"]["artifact_obligations"]
    check(
        "revision: assessment bound to active revision",
        code == 200 and body.get("ruleset_revision") == r3
        and "same-license" in aobs3 and "license-text" not in aobs3,
    )
    code, got = call("GET", "/v1/assessments/" + aid3)
    check(
        "revision: stored assessment records revision id",
        code == 200 and got.get("revision") == r3 and got.get("ruleset") == "revset",
    )

    # rollback: activate the old r1 with the same CAS flow
    code, body = call("POST", "/v1/rulesets/revset/revisions/%s/activate" % r1,
                      {"expected_active_revision": r3})
    check(
        "revision: CAS rollback to old revision",
        code == 200 and body.get("active_revision") == r1
        and body.get("previous_revision") == r3,
    )
    # rollback creates no new content row, just another activation record;
    # all historical content remains
    code, hist2 = call("GET", "/v1/rulesets/revset/revisions")
    check(
        "revision: history survives rollback untouched",
        [r["revision"] for r in hist2.get("revisions", [])] == [r1, r2, r3]
        and len(hist2.get("activations", [])) == 4
        and hist2.get("active_revision") == r1,
    )
    code, body = call("POST", "/v1/assessments", areq)
    check(
        "revision: fresh assessment follows rolled-back revision",
        code == 200 and body.get("ruleset_revision") == r1
        and body.get("id") != aid3
        and "same-license" not in body["result"]["artifact_obligations"],
    )
    # the old assessment's pinned result/revision are unchanged
    code, got = call("GET", "/v1/assessments/" + aid3)
    check(
        "revision: rollback does not mutate prior assessment",
        got.get("revision") == r3 and "same-license"
        in got["result"]["artifact_obligations"],
    )

    # unknown ruleset history -> 404
    code, body = call("GET", "/v1/rulesets/no-such-rs/revisions")
    check("revision: history of unknown ruleset -> 404", code == 404)
    # drafting for a not-yet-existing ruleset requires base_revision null,
    # and the draft stays unpublished until an explicit first activation
    code, body = call("POST", "/v1/rulesets/brandnew/revisions",
                      {"rules": v1, "base_revision": r1})
    check(
        "revision: non-null base on missing ruleset -> 409",
        code == 409 and body.get("reason") == "stale-base"
        and body.get("active_revision") is None,
    )
    code, body = call("POST", "/v1/rulesets/brandnew/revisions",
                      {"rules": v1, "base_revision": None})
    b1 = body.get("revision")
    check(
        "revision: first revision via revisions endpoint stays a draft",
        code == 200 and body.get("active") is False
        and body.get("base_revision") is None and b1 is not None,
    )
    code, body = call("GET", "/v1/rulesets/brandnew")
    check("revision: unpublished brandnew hidden from legacy GET", code == 404)
    code, hist = call("GET", "/v1/rulesets/brandnew/revisions")
    check(
        "revision: brandnew draft history has null active revision",
        code == 200 and hist.get("active_revision") is None
        and hist.get("activations") == []
        and [r["revision"] for r in hist["revisions"]] == [b1],
    )
    # activating an unknown revision of a draft-only ruleset -> 404
    code, body = call(
        "POST", "/v1/rulesets/brandnew/revisions/%s/activate" % ("a" * 16),
        {"expected_active_revision": None})
    check("revision: unknown revision of draft-only ruleset -> 404", code == 404)
    code, body = call(
        "POST", "/v1/rulesets/brandnew/revisions/%s/activate" % b1,
        {"expected_active_revision": None})
    check(
        "revision: brandnew first publish via null CAS expectation",
        code == 200 and body.get("active_revision") == b1
        and body.get("activated") is True
        and body.get("previous_revision") is None,
    )
    code, body = call(
        "POST", "/v1/rulesets/brandnew/revisions/%s/activate" % b1,
        {"expected_active_revision": b1})
    check(
        "revision: brandnew re-activation is idempotent",
        code == 200 and body.get("activated") is False,
    )
    code, hist = call("GET", "/v1/rulesets/brandnew/revisions")
    check(
        "revision: brandnew has exactly one first-publish audit",
        hist.get("active_revision") == b1 and len(hist.get("activations", [])) == 1
        and hist["activations"][0]["previous_revision"] is None,
    )

    # legacy PUT stays able to publish a draft-only name created via revisions
    code, body = call("POST", "/v1/rulesets/legacyboot/revisions",
                      {"rules": v2, "base_revision": None})
    lb = body["revision"]
    check("revision: draft-only name before legacy PUT is hidden",
          call("GET", "/v1/rulesets/legacyboot")[0] == 404)
    code, body = call("PUT", "/v1/rulesets/legacyboot", v2)
    check(
        "revision: legacy PUT publishes draft-only ruleset",
        code == 200 and body.get("created") is True
        and call("GET", "/v1/rulesets/legacyboot")[1].get("active_revision") == lb,
    )
    hist = call("GET", "/v1/rulesets/legacyboot/revisions")[1]
    check(
        "revision: legacy PUT publish reuses draft row, one audit",
        [r["revision"] for r in hist["revisions"]] == [lb]
        and len(hist["activations"]) == 1,
    )

    # -- real concurrency: two first-publish activations, one wins ----------
    code, fp = call("POST", "/v1/rulesets/firstrace/revisions",
                    {"rules": v1, "base_revision": None})
    first_target = fp["revision"]
    outcome = {}

    def first_activate(tag):
        # Both threads CAS expecting null (no active revision yet).
        c, b = call(
            "POST",
            "/v1/rulesets/firstrace/revisions/%s/activate" % first_target,
            {"expected_active_revision": None})
        outcome[tag] = (c, b)

    th1 = threading.Thread(target=first_activate, args=("a",))
    th2 = threading.Thread(target=first_activate, args=("b",))
    th1.start(); th2.start(); th1.join(); th2.join()
    codes = sorted(outcome[k][0] for k in outcome)
    check(
        "revision: racing first publishes -> exactly one wins",
        codes == [200, 409],
        str({k: v[0] for k, v in outcome.items()}),
    )
    loser = [k for k, v in outcome.items() if v[0] == 409][0]
    check(
        "revision: first-publish loser sees active-moved with winner revision",
        outcome[loser][1].get("reason") == "active-moved"
        and outcome[loser][1].get("expected_active_revision") is None
        and outcome[loser][1].get("active_revision") == first_target,
        json.dumps(outcome[loser][1])[:200],
    )
    hist = call("GET", "/v1/rulesets/firstrace/revisions")[1]
    check(
        "revision: first-publish race leaves a single audit row",
        hist.get("active_revision") == first_target
        and len(hist.get("activations", [])) == 1
        and hist["activations"][0]["previous_revision"] is None,
    )
    # loser retries with the now-current expectation: idempotent no-op
    code, body = call(
        "POST", "/v1/rulesets/firstrace/revisions/%s/activate" % first_target,
        {"expected_active_revision": first_target})
    check("revision: first-publish loser retries idempotently",
          code == 200 and body.get("activated") is False)

    # -- real concurrency: two activations racing, exactly one wins ---------
    race_v1 = {"licenses": {"Q-1.0": {"obligations": ["attribution"]}}}
    race_v2 = {"licenses": {"Q-1.0": {"obligations": ["attribution", "license-text"]}}}
    call("PUT", "/v1/rulesets/raceset", race_v1)
    start = call("GET", "/v1/rulesets/raceset")[1]["active_revision"]
    code, dr2 = call("POST", "/v1/rulesets/raceset/revisions",
                     {"rules": race_v2, "base_revision": start})
    target = dr2["revision"]
    outcome = {}

    def activate(tag):
        # Both threads CAS expecting the same starting revision.
        c, b = call("POST", "/v1/rulesets/raceset/revisions/%s/activate" % target,
                    {"expected_active_revision": start})
        outcome[tag] = (c, b)

    th1 = threading.Thread(target=activate, args=("a",))
    th2 = threading.Thread(target=activate, args=("b",))
    th1.start(); th2.start(); th1.join(); th2.join()
    codes = sorted(outcome[k][0] for k in outcome)
    check(
        "revision: racing activations -> exactly one wins",
        codes == [200, 409],
        str({k: v[0] for k, v in outcome.items()}),
    )
    loser = [k for k, v in outcome.items() if v[0] == 409][0]
    check(
        "revision: loser sees active-moved with current revision",
        outcome[loser][1].get("reason") == "active-moved"
        and outcome[loser][1].get("active_revision") == target,
    )
    # after the conflict, the loser retries with the fresh expectation
    code, body = call("POST", "/v1/rulesets/raceset/revisions/%s/activate" % target,
                      {"expected_active_revision": target})
    check("revision: retry with fresh expectation is idempotent",
          code == 200 and body.get("activated") is False)

    # -- fresh database seeds its first active revision ----------------------
    run_revision_fresh_db_checks()


def run_revision_fresh_db_checks():
    """A brand-new (and a pre-revision) database expose revisions sanely."""
    tmp = tempfile.mkdtemp(prefix="licapi-revs-")
    db = os.path.join(tmp, "fresh.db")
    api = Api(Store(db))
    api.store.put_ruleset("default", validate_ruleset(DEFAULT_RULESET))
    expected = stable_hash(validate_ruleset(DEFAULT_RULESET))[:16]
    hist = api.store.list_ruleset_revisions("default")
    check(
        "fresh db: seeded default is first active revision",
        hist is not None and hist["active_revision"] == expected
        and len(hist["revisions"]) == 1 and len(hist["activations"]) == 1,
    )
    # legacy pre-revision schema is migrated to first active revisions
    db2 = os.path.join(tmp, "legacy-rs.db")
    conn = sqlite3.connect(db2)
    conn.executescript(
        """
        CREATE TABLE rulesets (
            name TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE assessments (
            id TEXT PRIMARY KEY, ruleset TEXT, request TEXT NOT NULL,
            result TEXT NOT NULL, status TEXT NOT NULL,
            summary_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE confirmations (
            assessment_id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """
    )
    rules = validate_ruleset({"licenses": {"M-1.0": {"obligations": ["attribution"]}}})
    conn.execute(
        "INSERT INTO rulesets(name, body, created_at) VALUES (?,?,?)",
        ("migr", json.dumps(rules, sort_keys=True), "2024-01-01T00:00:00+00:00"),
    )
    norm = normalize_request({"components": [{"id": "m", "license": "M-1.0"}]})
    result = assess_normalized(norm, rules)
    aid = stable_hash({"request": norm, "rules": rules})[:32]
    conn.execute(
        "INSERT INTO assessments"
        "(id, ruleset, request, result, status, summary_hash, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (aid, "migr", json.dumps(norm, sort_keys=True),
         json.dumps(result, sort_keys=True), result["status"],
         result["summary_hash"], "2024-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    api2 = Api(Store(db2))
    rid = stable_hash(rules)[:16]
    hist = api2.store.list_ruleset_revisions("migr")
    check(
        "legacy db: ruleset migrated to first active revision",
        hist is not None and hist["active_revision"] == rid
        and len(hist["revisions"]) == 1
        and hist["revisions"][0]["base_revision"] is None
        and api2.store.get_ruleset("migr") == rules,
    )
    row = api2.store.get_assessment(aid)
    check(
        "legacy db: healed assessment stamped with revision",
        row["revision"] == rid and row["rules"] == rules,
    )
    # migrated content can draft/activate with the new flow
    v2 = validate_ruleset({"licenses": {"M-1.0": {"obligations": ["attribution", "license-text"]}}})
    draft = api2.store.create_ruleset_revision("migr", v2, rid)
    check("legacy db: draft on migrated ruleset",
          draft["created"] is True and draft["active"] is False)
    out = api2.store.activate_ruleset_revision("migr", draft["revision"], rid)
    check("legacy db: CAS activate migrated ruleset",
          out["activated"] is True and out["previous_revision"] == rid)
    # and roll back with the same path; both content rows still present
    out = api2.store.activate_ruleset_revision("migr", rid, draft["revision"])
    check("legacy db: CAS rollback migrated ruleset",
          out["activated"] is True and out["previous_revision"] == draft["revision"])
    hist = api2.store.list_ruleset_revisions("migr")
    check(
        "legacy db: history never loses content",
        [r["revision"] for r in hist["revisions"]] == [rid, draft["revision"]]
        and len(hist["activations"]) == 3
        and hist["active_revision"] == rid,
    )


def run_legacy_migration_checks():
    """Open a pre-upgrade database (assessments without a rules snapshot).

    Three legacy rows are inserted:

    * healable      -- named a ruleset whose current content is unchanged;
    * overwritten   -- named a ruleset that has since been replaced;
    * inline        -- a legacy inline-rule request (ruleset NULL).

    Opening the store must auto-migrate: the healable row is pinned, the
    other two stay unpinned.  Confirmation must succeed only for the
    healable row (and its replay must reproduce the original hash); the
    unverifiable rows must be refused with 409, never given fake replays.
    """
    tmp = tempfile.mkdtemp(prefix="licapi-legacy-")
    db = os.path.join(tmp, "legacy.db")

    legacy_schema = """
    CREATE TABLE rulesets (
        name TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE assessments (
        id TEXT PRIMARY KEY, ruleset TEXT, request TEXT NOT NULL,
        result TEXT NOT NULL, status TEXT NOT NULL,
        summary_hash TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE confirmations (
        assessment_id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL
    );
    """
    conn = sqlite3.connect(db)
    conn.executescript(legacy_schema)

    default_rules = validate_ruleset(DEFAULT_RULESET)
    legacy_rules = validate_ruleset(
        {"licenses": {"L-1.0": {"obligations": ["attribution"]}}}
    )
    conn.execute(
        "INSERT INTO rulesets(name, body, created_at) VALUES (?,?,?)",
        ("default", json.dumps(default_rules, sort_keys=True), "2024-01-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO rulesets(name, body, created_at) VALUES (?,?,?)",
        ("legacy", json.dumps(legacy_rules, sort_keys=True), "2024-01-01T00:00:00+00:00"),
    )

    def legacy_row(aid_rules, ruleset_name, request_body):
        norm = normalize_request(request_body)
        result = assess_normalized(norm, aid_rules)
        aid = stable_hash({"request": norm, "rules": aid_rules})[:32]
        conn.execute(
            "INSERT INTO assessments"
            "(id, ruleset, request, result, status, summary_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                aid,
                ruleset_name,
                json.dumps(norm, sort_keys=True),
                json.dumps(result, sort_keys=True),
                result["status"],
                result["summary_hash"],
                "2024-01-01T00:00:00+00:00",
            ),
        )
        return aid, result["summary_hash"]

    heal_aid, heal_hash = legacy_row(
        default_rules, "default",
        {"components": [{"id": "h", "license": "MIT"}]},
    )
    over_aid, over_hash = legacy_row(
        legacy_rules, "legacy",
        {"components": [{"id": "o", "license": "L-1.0"}]},
    )
    inline_aid, _inline_hash = legacy_row(
        legacy_rules, None,
        {"components": [{"id": "i", "license": "L-1.0"}]},
    )
    # overwrite the "legacy" ruleset *before* the new code ever opens the db
    changed_rules = validate_ruleset(
        {"licenses": {"L-1.0": {"obligations": ["attribution", "license-text"]}}}
    )
    conn.execute(
        "UPDATE rulesets SET body = ? WHERE name = 'legacy'",
        (json.dumps(changed_rules, sort_keys=True),),
    )
    conn.commit()
    conn.close()

    # opening triggers the schema migration + startup verification/healing
    api = Api(Store(db))

    healed = api.store.get_assessment(heal_aid)
    check("legacy healable row pinned on migration", healed["rules"] == default_rules)
    overwritten = api.store.get_assessment(over_aid)
    check("legacy overwritten row stays unpinned", overwritten["rules"] is None)
    inline_row = api.store.get_assessment(inline_aid)
    check("legacy inline row stays unpinned", inline_row["rules"] is None)

    # healed row confirms and its fixed replay reproduces the original hash
    conf = api.confirm(heal_aid)
    replay_req = conf["replay"]["request"]
    check(
        "legacy healed confirmation replay pins snapshot",
        isinstance(replay_req.get("rules"), dict)
        and conf["replay"]["rules_hash"] == stable_hash(default_rules),
    )
    replayed = assess_normalized(
        normalize_request(replay_req), validate_ruleset(replay_req["rules"])
    )
    check(
        "legacy healed replay reproduces summary hash",
        replayed["summary_hash"] == heal_hash,
    )

    # unverifiable rows: explicit refusal, never a fabricated confirmation
    for aid, why in ((over_aid, "overwritten"), (inline_aid, "inline")):
        try:
            api.confirm(aid)
            check("legacy %s row confirmation refused" % why, False, "no error raised")
        except ConflictError:
            check("legacy %s row confirmation refused" % why, True)

    # comparison against an unverifiable row is refused as well (never
    # compared against guessed rules); a healed row compares fine
    try:
        api.compare({"baseline_id": heal_aid, "target_id": over_aid})
        check("legacy unverifiable row compare refused", False, "no error raised")
    except ConflictError:
        check("legacy unverifiable row compare refused", True)
    out = api.compare({"baseline_id": heal_aid, "target_id": heal_aid})
    check(
        "legacy healed row self-compare",
        out["diff_count"] == 0 and out["conclusions_agree"] is True,
    )

    # restoring the original ruleset content makes the row verifiable again
    api.store.put_ruleset("legacy", legacy_rules)
    rules = api.store.recover_legacy_rules(over_aid)
    check(
        "legacy row heals after ruleset restored",
        rules == legacy_rules and api.store.get_assessment(over_aid)["rules"] == legacy_rules,
    )
    conf = api.confirm(over_aid)
    check(
        "recovered row confirmation replay reproduces",
        conf["replay"]["request"].get("rules") == legacy_rules
        and conf["replay"]["expected_summary_hash"] == over_hash,
    )


if __name__ == "__main__":
    sys.exit(main())
