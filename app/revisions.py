"""Immutable ruleset revisions: content hashing and revision identity.

A revision is the *normalized* ruleset content (see
``rules.validate_ruleset``) frozen under a SHA-256 content hash.  Equal
normalized content always yields the same revision id, so drafts are
deduplicated by content and a rollback to old rules lands on the same
revision row they originally produced.

Revision ids are the leading ``REVISION_ID_LEN`` hex chars of the content
hash -- long enough to be treated as opaque ids in URLs while the full
hash remains available for verification.
"""

from .common import stable_hash

REVISION_ID_LEN = 16


def rules_content_hash(rules):
    """Full SHA-256 hex digest of the normalized ruleset content."""
    return stable_hash(rules)


def revision_id(rules):
    """Short, URL-safe content id of the normalized ruleset content."""
    return rules_content_hash(rules)[:REVISION_ID_LEN]
