"""Obligation rulesets: default content, validation and normalization.

A ruleset is a JSON object::

    {
      "licenses": {
        "<license-id>": {
          "obligations":       ["attribution", ...],   # duties when included
          "propagates":        ["static", "dynamic"],  # linkage kinds that
                                                       # carry duties upstream
          "incompatible_with": ["<license-id>", ...]   # conflicting licenses
        }
      },
      "exceptions": {
        "<exception-id>": {
          "removes":             ["same-license"],
          "adds":                [],
          "removes_propagation": true
        }
      },
      "obligation_scopes": {
        "source-offer": ["binary", "source"]           # scopes where the
                                                       # obligation applies
      },
      "combined_work_linkages": ["static", "dynamic"]  # linkages that merge
                                                       # components into one
                                                       # combined work
    }

Obligations are an open vocabulary; the built-in ruleset uses:
attribution, license-text, notice-preserve, no-endorsement, source-offer,
same-license, network-source, relink-notice.
"""

from .common import DISTRIBUTIONS, LINKAGES, ClientError

DEFAULT_RULESET = {
    "licenses": {
        "MIT": {
            "obligations": ["attribution", "license-text"],
            "propagates": [],
            "incompatible_with": [],
        },
        "ISC": {
            "obligations": ["attribution", "license-text"],
            "propagates": [],
            "incompatible_with": [],
        },
        "BSD-2-Clause": {
            "obligations": ["attribution", "license-text"],
            "propagates": [],
            "incompatible_with": [],
        },
        "BSD-3-Clause": {
            "obligations": ["attribution", "license-text", "no-endorsement"],
            "propagates": [],
            "incompatible_with": [],
        },
        "Apache-2.0": {
            "obligations": ["attribution", "license-text", "notice-preserve"],
            "propagates": [],
            "incompatible_with": ["GPL-2.0-only"],
        },
        "GPL-2.0-only": {
            "obligations": ["attribution", "license-text", "source-offer", "same-license"],
            "propagates": ["static", "dynamic"],
            "incompatible_with": ["Apache-2.0", "GPL-3.0-only", "LGPL-3.0-only", "AGPL-3.0-only"],
        },
        "GPL-3.0-only": {
            "obligations": ["attribution", "license-text", "source-offer", "same-license"],
            "propagates": ["static", "dynamic"],
            "incompatible_with": ["GPL-2.0-only", "LGPL-2.1-only"],
        },
        "LGPL-2.1-only": {
            "obligations": ["attribution", "license-text", "source-offer", "relink-notice"],
            "propagates": ["static"],
            "incompatible_with": ["GPL-3.0-only"],
        },
        "LGPL-3.0-only": {
            "obligations": ["attribution", "license-text", "source-offer", "relink-notice"],
            "propagates": ["static"],
            "incompatible_with": ["GPL-2.0-only"],
        },
        "AGPL-3.0-only": {
            "obligations": [
                "attribution",
                "license-text",
                "source-offer",
                "same-license",
                "network-source",
            ],
            "propagates": ["static", "dynamic"],
            "incompatible_with": ["GPL-2.0-only"],
        },
        "MPL-2.0": {
            "obligations": ["attribution", "license-text", "source-offer"],
            "propagates": [],
            "incompatible_with": ["GPL-2.0-only"],
        },
        "EPL-2.0": {
            "obligations": ["attribution", "license-text", "source-offer"],
            "propagates": [],
            "incompatible_with": ["GPL-2.0-only", "GPL-3.0-only"],
        },
        "CC0-1.0": {
            "obligations": [],
            "propagates": [],
            "incompatible_with": [],
        },
        "Unlicense": {
            "obligations": [],
            "propagates": [],
            "incompatible_with": [],
        },
    },
    "exceptions": {
        "Classpath-exception-2.0": {
            "removes": ["same-license"],
            "adds": [],
            "removes_propagation": True,
        },
        "LLVM-exception": {
            "removes": [],
            "adds": [],
            "removes_propagation": True,
        },
    },
    "obligation_scopes": {
        "source-offer": ["binary", "source"],
        "network-source": ["saas"],
        "relink-notice": ["binary"],
    },
    "combined_work_linkages": ["static", "dynamic"],
}


def _str_list(value, where):
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ClientError("%s must be an array of strings" % where)
    return value


def validate_ruleset(body):
    """Validate a ruleset payload and return a normalized copy.

    Normalization sorts every list so equal rulesets serialize (and hash)
    identically regardless of key/element order.
    """
    if not isinstance(body, dict):
        raise ClientError("ruleset must be a JSON object")
    licenses = body.get("licenses")
    if not isinstance(licenses, dict) or not licenses:
        raise ClientError("ruleset.licenses must be a non-empty object")

    norm_licenses = {}
    for lid in sorted(licenses):
        if not isinstance(lid, str) or not lid:
            raise ClientError("license ids must be non-empty strings")
        rule = licenses[lid]
        if not isinstance(rule, dict):
            raise ClientError("ruleset.licenses[%r] must be an object" % lid)
        obligations = _str_list(rule.get("obligations", []), "licenses[%s].obligations" % lid)
        propagates = _str_list(rule.get("propagates", []), "licenses[%s].propagates" % lid)
        bad = [p for p in propagates if p not in LINKAGES]
        if bad:
            raise ClientError(
                "licenses[%s].propagates contains unknown linkage %r (allowed: %s)"
                % (lid, bad[0], list(LINKAGES))
            )
        incompatible = _str_list(
            rule.get("incompatible_with", []), "licenses[%s].incompatible_with" % lid
        )
        norm_licenses[lid] = {
            "obligations": sorted(set(obligations)),
            "propagates": sorted(set(propagates)),
            "incompatible_with": sorted(set(incompatible)),
        }

    exceptions = body.get("exceptions", {})
    if not isinstance(exceptions, dict):
        raise ClientError("ruleset.exceptions must be an object")
    norm_exceptions = {}
    for eid in sorted(exceptions):
        exc = exceptions[eid]
        if not isinstance(exc, dict):
            raise ClientError("ruleset.exceptions[%r] must be an object" % eid)
        norm_exceptions[eid] = {
            "removes": sorted(set(_str_list(exc.get("removes", []), "exceptions[%s].removes" % eid))),
            "adds": sorted(set(_str_list(exc.get("adds", []), "exceptions[%s].adds" % eid))),
            "removes_propagation": bool(exc.get("removes_propagation", False)),
        }

    scopes = body.get("obligation_scopes", {})
    if not isinstance(scopes, dict):
        raise ClientError("ruleset.obligation_scopes must be an object")
    norm_scopes = {}
    for ob in sorted(scopes):
        vals = _str_list(scopes[ob], "obligation_scopes[%s]" % ob)
        bad = [v for v in vals if v not in DISTRIBUTIONS]
        if bad:
            raise ClientError(
                "obligation_scopes[%s] contains unknown scope %r (allowed: %s)"
                % (ob, bad[0], list(DISTRIBUTIONS))
            )
        norm_scopes[ob] = sorted(set(vals))

    combined = body.get("combined_work_linkages", ["static", "dynamic"])
    combined = _str_list(combined, "combined_work_linkages")
    bad = [v for v in combined if v not in LINKAGES]
    if bad:
        raise ClientError(
            "combined_work_linkages contains unknown linkage %r (allowed: %s)"
            % (bad[0], list(LINKAGES))
        )

    return {
        "licenses": norm_licenses,
        "exceptions": norm_exceptions,
        "obligation_scopes": norm_scopes,
        "combined_work_linkages": sorted(set(combined)),
    }
