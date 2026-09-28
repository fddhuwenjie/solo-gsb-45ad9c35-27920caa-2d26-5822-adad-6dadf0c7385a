"""Shared constants, error types and canonical-JSON helpers."""

import hashlib
import json

# Linkage kinds a component may use when incorporated by its dependents.
LINKAGES = ("static", "dynamic", "process", "none")

# Distribution scopes an assessment may target.
DISTRIBUTIONS = ("internal", "binary", "source", "saas")


class ClientError(ValueError):
    """The request is invalid (mapped to HTTP 400)."""


class NotFound(LookupError):
    """The requested resource does not exist (mapped to HTTP 404)."""


class ConflictError(RuntimeError):
    """The request conflicts with the current state (mapped to HTTP 409).

    Optional ``details`` are merged into the JSON error body so callers
    can tell distinct conflict kinds apart (a lost activation race versus
    an expired revision baseline, etc.).
    """

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = dict(details) if details else None


def canonical(obj):
    """Serialize to canonical JSON (sorted keys, tight separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_hash(obj):
    """SHA-256 over the canonical JSON form of *obj*.

    Because dicts are serialized with sorted keys and all lists in the
    payload are produced in sorted order, the hash is independent of the
    input ordering of the original request.
    """
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()
