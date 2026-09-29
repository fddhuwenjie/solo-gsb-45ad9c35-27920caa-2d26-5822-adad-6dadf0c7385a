"""SQLite persistence layer (stdlib sqlite3, WAL mode, lock-guarded writes)."""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from .common import ConflictError, NotFound, stable_hash
from .revisions import revision_id
from .solver import assess_normalized

# user_version history:
#   0 -- pre-revision databases (rulesets table + optional rules snapshot)
#   2 -- immutable revisions: ruleset_revisions / ruleset_active /
#        ruleset_activations and assessments.revision provenance
#   3 -- draft gating: ruleset_active.revision / activated_at become
#        nullable.  A ruleset's first revision is now an *unpublished*
#        draft: its name is reserved by a (name, NULL, NULL) pointer and
#        the first activation fills that pointer atomically.  Rulesets
#        migrated at version 2 keep their non-null, already-active row.
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS rulesets (
    name       TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ruleset_revisions (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ruleset_name TEXT NOT NULL,
    revision     TEXT NOT NULL,
    body         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    base_revision TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (ruleset_name, revision)
);
CREATE TABLE IF NOT EXISTS ruleset_active (
    ruleset_name TEXT PRIMARY KEY,
    revision     TEXT,
    activated_at TEXT,
    FOREIGN KEY (ruleset_name, revision)
        REFERENCES ruleset_revisions(ruleset_name, revision)
);
CREATE TABLE IF NOT EXISTS ruleset_activations (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ruleset_name TEXT NOT NULL,
    revision     TEXT NOT NULL,
    previous_revision TEXT,
    activated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments (
    id           TEXT PRIMARY KEY,
    ruleset      TEXT,
    rules        TEXT,
    revision     TEXT,
    request      TEXT NOT NULL,
    result       TEXT NOT NULL,
    status       TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations (
    assessment_id TEXT PRIMARY KEY,
    body          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rev_label(rev):
    """Render a revision id for human-readable errors (None -> 'null')."""
    return "null" if rev is None else repr(rev)


class Store:
    """Thread-safe store; a fresh connection is used per operation."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.commit()
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # -- schema migration -------------------------------------------------

    def _migrate(self, conn):
        """Upgrade older databases across the migration steps.

        Assessment snapshot healing (any database): pin rules snapshots
        onto legacy assessment rows whose original rules can be *proven*
        identical (see :meth:`_verified_legacy_rules`).

        user_version < 2: introduce immutable revisions.  Every legacy
        ``rulesets`` row becomes the first active revision of that
        ruleset (the revision id is the content hash, so future identical
        PUTs/drafts collapse onto it), and each assessment is stamped
        with the revision of its pinned snapshot whenever it can be
        derived.  Neither move loses or rewrites historical content.

        user_version < 3: make the active pointer nullable by rebuilding
        ``ruleset_active``, so a not-yet-published ruleset can reserve its
        name with a NULL pointer and publish its first revision via CAS.
        Existing rows (all non-null after the version-2 step) are copied
        verbatim; no activation semantics change for stored data.
        """
        self._migrate_assessment_snapshots(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            self._migrate_revisions(conn)
            version = 2
            conn.execute("PRAGMA user_version=2")
        if version < 3:
            self._migrate_nullable_active(conn)
            version = 3
            conn.execute("PRAGMA user_version=3")
        if version != SCHEMA_VERSION:
            conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)

    @staticmethod
    def _active_columns(conn):
        return {
            r[1]: r
            for r in conn.execute("PRAGMA table_info(ruleset_active)")
        }

    def _migrate_nullable_active(self, conn):
        """Rebuild ``ruleset_active`` with a nullable revision pointer.

        SQLite cannot ALTER a column's NOT NULL constraint, so the table
        is rebuilt.  Every stored pointer (and its ``activated_at``) is
        copied across unchanged; the foreign-key parent rows are all
        present, so a deferred-checks rebuild stays consistent.
        """
        cols = self._active_columns(conn)
        rev = cols.get("revision")
        # notnull flag (column 3) is 1 for the original NOT NULL column.
        if rev is not None and not rev[3]:
            return
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("ALTER TABLE ruleset_active RENAME TO ruleset_active_v2")
            conn.execute(
                "CREATE TABLE ruleset_active ("
                " ruleset_name TEXT PRIMARY KEY,"
                " revision TEXT,"
                " activated_at TEXT,"
                " FOREIGN KEY (ruleset_name, revision)"
                "  REFERENCES ruleset_revisions(ruleset_name, revision))"
            )
            conn.execute(
                "INSERT INTO ruleset_active(ruleset_name, revision, activated_at)"
                " SELECT ruleset_name, revision, activated_at FROM ruleset_active_v2"
            )
            conn.execute("DROP TABLE ruleset_active_v2")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        bad = conn.execute(
            "PRAGMA foreign_key_check(ruleset_active)"
        ).fetchall()
        if bad:  # pragma: no cover - migration preserves every parent row
            raise RuntimeError("ruleset_active migration broke referential integrity")

    def _migrate_assessment_snapshots(self, conn):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(assessments)")]
        if "rules" not in cols:
            conn.execute("ALTER TABLE assessments ADD COLUMN rules TEXT")
        rows = conn.execute(
            "SELECT id, ruleset, request, result FROM assessments WHERE rules IS NULL"
        ).fetchall()
        for row in rows:
            rules = self._verified_legacy_rules(conn, row)
            if rules is not None:
                conn.execute(
                    "UPDATE assessments SET rules = ? WHERE id = ?",
                    (json.dumps(rules, sort_keys=True), row["id"]),
                )

    def _migrate_revisions(self, conn):
        if "revision" not in [
            r[1] for r in conn.execute("PRAGMA table_info(assessments)")
        ]:
            conn.execute("ALTER TABLE assessments ADD COLUMN revision TEXT")

        # Every existing ruleset becomes its own first, active revision.
        # Content is moved verbatim and never rewritten: the revision id
        # is the hash of the stored (already normalized) body.
        for rs in conn.execute(
            "SELECT name, body, created_at FROM rulesets"
        ).fetchall():
            rules = json.loads(rs["body"])
            rid = revision_id(rules)
            conn.execute(
                "INSERT OR IGNORE INTO ruleset_revisions"
                "(ruleset_name, revision, body, content_hash, base_revision, created_at)"
                " VALUES (?,?,?,?,NULL,?)",
                (rs["name"], rid, rs["body"], stable_hash(rules), rs["created_at"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO ruleset_active(ruleset_name, revision, activated_at)"
                " VALUES (?,?,?)",
                (rs["name"], rid, rs["created_at"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO ruleset_activations"
                "(ruleset_name, revision, previous_revision, activated_at)"
                " VALUES (?,?,NULL,?)",
                (rs["name"], rid, rs["created_at"]),
            )

        # Stamp assessments with the revision of the pinned snapshot when
        # the (ruleset, revision) pair exists.  Unverifiable rows stay NULL.
        for row in conn.execute(
            "SELECT id, ruleset, rules FROM assessments WHERE revision IS NULL AND rules IS NOT NULL"
        ).fetchall():
            if not row["ruleset"]:
                continue
            rules = json.loads(row["rules"])
            rid = revision_id(rules)
            hit = conn.execute(
                "SELECT 1 FROM ruleset_revisions WHERE ruleset_name = ? AND revision = ?",
                (row["ruleset"], rid),
            ).fetchone()
            if hit is not None:
                conn.execute(
                    "UPDATE assessments SET revision = ? WHERE id = ?", (rid, row["id"])
                )

    @staticmethod
    def _verified_legacy_rules(conn, row):
        """Return a legacy row's original rules only when they are provable.

        A snapshot is accepted when all of the following hold:

        1. the row named a stored ruleset (inline-rule requests were never
           persisted, so they can never be restored),
        2. a ruleset with that name still exists and its current, normalized
           content hashes to the assessment id stored on the row,
        3. re-running the assessment with those rules reproduces the stored
           ``summary_hash`` exactly.

        Anything else -- a deleted/overwritten ruleset, an inline-rule row,
        any hash mismatch -- yields None; callers must then refuse to mint a
        confirmation rather than inventing a replay.
        """
        name = row["ruleset"]
        if not name:
            return None
        rs = conn.execute("SELECT body FROM rulesets WHERE name = ?", (name,)).fetchone()
        if rs is None:
            return None
        rules = json.loads(rs["body"])
        request = json.loads(row["request"])
        if stable_hash({"request": request, "rules": rules})[:32] != row["id"]:
            return None
        stored = json.loads(row["result"])
        replayed = assess_normalized(request, rules)
        if replayed.get("summary_hash") != stored.get("summary_hash"):
            return None
        return rules

    def recover_legacy_rules(self, aid):
        """Best-effort recovery of a pre-upgrade assessment's rules.

        Re-runs verification on demand (the ruleset may have been restored
        after startup).  Backfills and returns the normalized snapshot on
        success, otherwise None -- never a guessed payload.
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, ruleset, rules, request, result"
                    " FROM assessments WHERE id = ?",
                    (aid,),
                ).fetchone()
                if row is None:
                    return None
                if row["rules"] is not None:
                    return json.loads(row["rules"])
                rules = self._verified_legacy_rules(conn, row)
                if rules is not None:
                    rid = revision_id(rules)
                    hit = conn.execute(
                        "SELECT 1 FROM ruleset_revisions"
                        " WHERE ruleset_name = ? AND revision = ?",
                        (row["ruleset"], rid),
                    ).fetchone()
                    if hit is not None:
                        conn.execute(
                            "UPDATE assessments SET rules = ?, revision = ? WHERE id = ?",
                            (json.dumps(rules, sort_keys=True), rid, aid),
                        )
                    else:
                        conn.execute(
                            "UPDATE assessments SET rules = ? WHERE id = ?",
                            (json.dumps(rules, sort_keys=True), aid),
                        )
                    conn.commit()
                return rules
            finally:
                conn.close()

    # -- rulesets (legacy PUT/GET semantics, revision-backed) -------------

    def put_ruleset(self, name, rules):
        """Create or replace the *active* content (legacy PUT semantics).

        Backed by immutable revisions: new content appends a revision and
        atomically flips the active pointer; identical content is a no-op
        and destroys nothing.  Returns ``created`` (nothing was published
        under the name before, including a brand-new name or one holding
        only an unpublished draft).  PUT on a draft-only ruleset performs
        the first publication for legacy clients: the draft rows are left
        untouched and only the active pointer, its audit row and the
        legacy mirror are written.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                active = self._active_row(conn, name)
                body = json.dumps(rules, sort_keys=True)
                rid = revision_id(rules)
                now = _now()
                created = False
                if active is None or active["revision"] is None:
                    # First publication.  A brand-new name needs its
                    # revision row; a draft-only name may already hold one
                    # (inserted by the revisions endpoint) -- in that case
                    # INSERT OR IGNORE collapses onto it and nothing
                    # historical is rewritten.
                    created = active is None or active["revision"] is None
                    conn.execute(
                        "INSERT OR IGNORE INTO ruleset_revisions"
                        "(ruleset_name, revision, body, content_hash, base_revision, created_at)"
                        " VALUES (?,?,?,?,NULL,?)",
                        (name, rid, body, stable_hash(rules), now),
                    )
                    if active is None:
                        conn.execute(
                            "INSERT INTO ruleset_active(ruleset_name, revision, activated_at)"
                            " VALUES (?,?,?)",
                            (name, rid, now),
                        )
                    else:
                        # Fill the reserved NULL pointer in the same CAS
                        # style the explicit activation endpoint uses.
                        conn.execute(
                            "UPDATE ruleset_active SET revision = ?, activated_at = ?"
                            " WHERE ruleset_name = ? AND revision IS NULL",
                            (rid, now, name),
                        )
                    conn.execute(
                        "INSERT INTO ruleset_activations"
                        "(ruleset_name, revision, previous_revision, activated_at)"
                        " VALUES (?,?,NULL,?)",
                        (name, rid, now),
                    )
                elif active["revision"] != rid:
                    # Legacy PUT is unconditional: its base is the content
                    # currently active at the moment of the write.
                    self._append_and_activate(conn, name, active, rules, body, rid, now)
                conn.execute(
                    "INSERT OR REPLACE INTO rulesets(name, body, created_at) VALUES (?,?,?)",
                    (name, body, now),
                )
                conn.commit()
                return created
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def get_ruleset(self, name):
        """Active ruleset content (legacy GET mirror)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT r.body AS body FROM ruleset_active a"
                " JOIN ruleset_revisions r"
                " ON r.ruleset_name = a.ruleset_name AND r.revision = a.revision"
                " WHERE a.ruleset_name = ?",
                (name,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT body FROM rulesets WHERE name = ?", (name,)
                ).fetchone()
        finally:
            conn.close()
        return json.loads(row["body"]) if row else None

    def list_rulesets(self):
        conn = self._connect()
        try:
            # Only *published* rulesets are listed: a name whose pointer is
            # NULL still holds unpublished drafts and must stay invisible
            # to legacy consumers (old GET / named-rule accounting).
            rows = conn.execute(
                "SELECT ruleset_name FROM ruleset_active"
                " WHERE revision IS NOT NULL ORDER BY ruleset_name"
            ).fetchall()
        finally:
            conn.close()
        return [r["ruleset_name"] for r in rows]

    # -- immutable revisions & atomic activation --------------------------

    @staticmethod
    def _active_row(conn, name):
        return conn.execute(
            "SELECT ruleset_name, revision, activated_at"
            " FROM ruleset_active WHERE ruleset_name = ?",
            (name,),
        ).fetchone()

    @staticmethod
    def _append_and_activate(conn, name, active, rules, body, rid, now=None):
        """Append (if new) a revision and switch the active pointer to it.

        Must run inside an already-open transaction; the active row read at
        transaction start is what gets recorded as ``previous_revision``.
        """
        if now is None:
            now = _now()
        conn.execute(
            "INSERT OR IGNORE INTO ruleset_revisions"
            "(ruleset_name, revision, body, content_hash, base_revision, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (name, rid, body, stable_hash(rules), active["revision"], now),
        )
        conn.execute(
            "UPDATE ruleset_active SET revision = ?, activated_at = ?"
            " WHERE ruleset_name = ? AND revision = ?",
            (rid, now, name, active["revision"]),
        )
        conn.execute(
            "INSERT INTO ruleset_activations"
            "(ruleset_name, revision, previous_revision, activated_at)"
            " VALUES (?,?,?,?)",
            (name, rid, active["revision"], now),
        )
        # Keep the legacy mirror in sync for old GET/PUT consumers.
        conn.execute(
            "INSERT OR REPLACE INTO rulesets(name, body, created_at) VALUES (?,?,?)",
            (name, body, now),
        )

    def create_ruleset_revision(self, name, rules, base_revision):
        """Create a content-deduplicated, *inactive* draft revision.

        Drafts never take effect on their own: publication is a separate
        CAS step (see :meth:`activate_ruleset_revision`).  Optimistic
        concurrency on subsequent revisions: ``base_revision`` must equal
        the active revision as of the transaction start; an expired
        baseline raises ConflictError.

        For a not-yet-existing ruleset ``base_revision`` must be ``None``;
        the first call stores the immutable draft and *reserves the name*
        with a NULL active pointer (no activation audit, no legacy mirror),
        so the response carries ``active: false`` and history reports
        ``active_revision: null`` until the first activation.  Drafting
        more content for such a ruleset needs an existing revision of it
        as the base (still unpublished); ``null`` only applies to the
        initial draft.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                active = self._active_row(conn, name)
                body = json.dumps(rules, sort_keys=True)
                rid = revision_id(rules)
                now = _now()
                if active is None:
                    if base_revision is not None:
                        raise ConflictError(
                            "stale base_revision for %r: ruleset does not exist yet"
                            % name,
                            {
                                "reason": "stale-base",
                                "base_revision": base_revision,
                                "active_revision": None,
                            },
                        )
                    conn.execute(
                        "INSERT INTO ruleset_revisions"
                        "(ruleset_name, revision, body, content_hash, base_revision, created_at)"
                        " VALUES (?,?,?,?,NULL,?)",
                        (name, rid, body, stable_hash(rules), now),
                    )
                    # Reserve the name without publishing anything: the
                    # NULL pointer is what keeps the draft out of legacy
                    # GET and named-rule accounting until activation.
                    conn.execute(
                        "INSERT INTO ruleset_active(ruleset_name, revision, activated_at)"
                        " VALUES (?,NULL,NULL)",
                        (name,),
                    )
                    created = True
                    stored_base, stored_at = None, now
                    active_rev = None
                else:
                    active_rev = active["revision"]  # None while unpublished
                    if active_rev is not None:
                        valid_base = base_revision == active_rev
                    else:
                        # Unpublished ruleset: the first draft required
                        # null; every later draft must branch from a
                        # revision that already belongs to the ruleset.
                        valid_base = base_revision is not None and conn.execute(
                            "SELECT 1 FROM ruleset_revisions"
                            " WHERE ruleset_name = ? AND revision = ?",
                            (name, base_revision),
                        ).fetchone() is not None
                    if not valid_base:
                        raise ConflictError(
                            "stale base_revision for %r: active revision is %s"
                            % (name, _rev_label(active_rev)),
                            {
                                "reason": "stale-base",
                                "base_revision": base_revision,
                                "active_revision": active_rev,
                            },
                        )
                    existing = conn.execute(
                        "SELECT created_at, base_revision FROM ruleset_revisions"
                        " WHERE ruleset_name = ? AND revision = ?",
                        (name, rid),
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            "INSERT INTO ruleset_revisions"
                            "(ruleset_name, revision, body, content_hash, base_revision, created_at)"
                            " VALUES (?,?,?,?,?,?)",
                            (name, rid, body, stable_hash(rules), base_revision, now),
                        )
                        created, stored_base, stored_at = True, base_revision, now
                    else:
                        created = False
                        stored_base = existing["base_revision"]
                        stored_at = existing["created_at"]
                conn.commit()
                return {
                    "name": name,
                    "revision": rid,
                    "content_hash": stable_hash(rules),
                    "base_revision": stored_base,
                    "created_at": stored_at,
                    "active": rid == active_rev,
                    "created": created,
                }
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def activate_ruleset_revision(self, name, rid, expected_active):
        """Atomically switch (or publish) the active revision via CAS.

        The active pointer is (re-)read inside one transaction and the
        switch/fill, its audit row, the previous-revision provenance and
        the legacy mirror update all commit together.  A lost race (the
        pointer moved since the caller fetched it) raises ConflictError
        and changes nothing.

        ``expected_active`` of ``None`` means "publish the first
        revision": it matches only a reserved NULL pointer, so two
        concurrent first-publications still resolve to one 200 and one
        409.  Activating the revision already active is idempotent and
        adds no audit row.  Activating an unknown revision is 404.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                active = self._active_row(conn, name)
                if active is None:
                    raise NotFound("ruleset %r not found" % name)
                current = active["revision"]  # None while unpublished
                target = conn.execute(
                    "SELECT body FROM ruleset_revisions WHERE ruleset_name = ? AND revision = ?",
                    (name, rid),
                ).fetchone()
                if target is None:
                    raise NotFound(
                        "revision %r of ruleset %r not found" % (rid, name)
                    )
                if current != expected_active:
                    raise ConflictError(
                        "expected active revision %s but it is %s"
                        % (_rev_label(expected_active), _rev_label(current)),
                        {
                            "reason": "active-moved",
                            "expected_active_revision": expected_active,
                            "active_revision": current,
                        },
                    )
                if current == rid:
                    # Idempotent re-activation: unchanged timestamp, and
                    # no new audit row.  Only reachable with a non-null
                    # pointer, since the target lookup above guarantees
                    # rid is not None.
                    result = {
                        "name": name,
                        "revision": rid,
                        "active_revision": rid,
                        "activated": False,
                        "previous_revision": rid,
                        "activated_at": active["activated_at"],
                    }
                    conn.commit()
                    return result
                now = _now()
                if current is None:
                    # First publication: fill the reserved NULL pointer.
                    updated = conn.execute(
                        "UPDATE ruleset_active SET revision = ?, activated_at = ?"
                        " WHERE ruleset_name = ? AND revision IS NULL",
                        (rid, now, name),
                    )
                    if updated.rowcount != 1:  # pragma: no cover - lock-held guard
                        raise ConflictError(
                            "first publication of %r lost the active pointer" % name,
                            {"reason": "active-moved", "active_revision": None},
                        )
                else:
                    conn.execute(
                        "UPDATE ruleset_active SET revision = ?, activated_at = ?"
                        " WHERE ruleset_name = ? AND revision = ?",
                        (rid, now, name, current),
                    )
                conn.execute(
                    "INSERT INTO ruleset_activations"
                    "(ruleset_name, revision, previous_revision, activated_at)"
                    " VALUES (?,?,?,?)",
                    (name, rid, current, now),
                )
                # Only a published revision is mirrored for old GET/PUT
                # consumers; drafts never touch the legacy rulesets table.
                conn.execute(
                    "INSERT OR REPLACE INTO rulesets(name, body, created_at)"
                    " VALUES (?,?,?)",
                    (name, target["body"], now),
                )
                conn.commit()
                return {
                    "name": name,
                    "revision": rid,
                    "active_revision": rid,
                    "activated": True,
                    "previous_revision": current,
                    "activated_at": now,
                }
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def list_ruleset_revisions(self, name):
        """Return the immutable revision history plus activation records.

        A ruleset that only holds drafts (never activated) still has
        history -- its revisions are listed with ``active: false`` and
        ``active_revision`` is ``None``; only a completely unknown name
        returns None so callers can 404.
        """
        conn = self._connect()
        try:
            active = self._active_row(conn, name)
            revs = conn.execute(
                "SELECT revision, body, content_hash, base_revision, created_at"
                " FROM ruleset_revisions WHERE ruleset_name = ?"
                " ORDER BY seq",
                (name,),
            ).fetchall()
            if active is None and not revs:
                return None
            activations = conn.execute(
                "SELECT revision, previous_revision, activated_at"
                " FROM ruleset_activations WHERE ruleset_name = ?"
                " ORDER BY seq",
                (name,),
            ).fetchall()
            current = active["revision"] if active is not None else None
            result = {
                "name": name,
                "active_revision": current,
                "revisions": [
                    {
                        "revision": r["revision"],
                        "content_hash": r["content_hash"],
                        "base_revision": r["base_revision"],
                        "active": r["revision"] == current,
                        "created_at": r["created_at"],
                        "ruleset": json.loads(r["body"]),
                    }
                    for r in revs
                ],
                "activations": [
                    {
                        "revision": a["revision"],
                        "previous_revision": a["previous_revision"],
                        "activated_at": a["activated_at"],
                    }
                    for a in activations
                ],
            }
        finally:
            conn.close()
        return result

    # -- assessments ------------------------------------------------------

    def put_assessment(self, aid, ruleset, rules, request, result, revision=None):
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO assessments"
                    "(id, ruleset, rules, revision, request, result, status,"
                    " summary_hash, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        aid,
                        ruleset,
                        json.dumps(rules, sort_keys=True),
                        revision,
                        json.dumps(request, sort_keys=True),
                        json.dumps(result, sort_keys=True),
                        result.get("status", ""),
                        result.get("summary_hash", ""),
                        _now(),
                    ),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def create_named_assessment(self, ruleset_name, norm):
        """Evaluate against the active revision inside one transaction.

        The active revision, its ruleset body and the audit-relevant
        revision id are read at transaction start, the assessment is
        evaluated against that snapshot and the row (carrying the exact
        revision id it ran with) is written in the same transaction, so
        the recorded ``revision`` is provably the one that was active when
        the accounting began -- an activation racing with the assessment
        cannot make the row claim a different revision.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    "SELECT revision FROM ruleset_active WHERE ruleset_name = ?",
                    (ruleset_name,),
                ).fetchone()
                # No row at all, or only an unpublished draft (NULL
                # pointer): named-rule accounting may never see it.
                if active is None or active["revision"] is None:
                    raise NotFound("ruleset %r not found" % ruleset_name)
                rev_row = conn.execute(
                    "SELECT body FROM ruleset_revisions"
                    " WHERE ruleset_name = ? AND revision = ?",
                    (ruleset_name, active["revision"]),
                ).fetchone()
                rules = json.loads(rev_row["body"])
                aid = stable_hash({"request": norm, "rules": rules})[:32]
                row = conn.execute(
                    "SELECT result FROM assessments WHERE id = ?", (aid,)
                ).fetchone()
                if row is not None:
                    conn.commit()
                    return {
                        "id": aid,
                        "created": False,
                        "result": json.loads(row["result"]),
                        "revision": active["revision"],
                    }
                result = assess_normalized(norm, rules)
                conn.execute(
                    "INSERT INTO assessments"
                    "(id, ruleset, rules, revision, request, result, status,"
                    " summary_hash, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        aid,
                        ruleset_name,
                        json.dumps(rules, sort_keys=True),
                        active["revision"],
                        json.dumps(norm, sort_keys=True),
                        json.dumps(result, sort_keys=True),
                        result.get("status", ""),
                        result.get("summary_hash", ""),
                        _now(),
                    ),
                )
                conn.commit()
                return {
                    "id": aid,
                    "created": True,
                    "result": result,
                    "revision": active["revision"],
                }
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def get_assessment(self, aid):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM assessments WHERE id = ?", (aid,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "id": row["id"],
            "ruleset": row["ruleset"],
            "rules": json.loads(row["rules"]) if row["rules"] is not None else None,
            "revision": row["revision"],
            "request": json.loads(row["request"]),
            "result": json.loads(row["result"]),
            "created_at": row["created_at"],
        }

    # -- confirmations ----------------------------------------------------

    def put_confirmation(self, aid, body):
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO confirmations(assessment_id, body, created_at)"
                    " VALUES (?,?,?)",
                    (aid, json.dumps(body, sort_keys=True), _now()),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_confirmation(self, aid):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT body FROM confirmations WHERE assessment_id = ?", (aid,)
            ).fetchone()
        finally:
            conn.close()
        return json.loads(row["body"]) if row else None
