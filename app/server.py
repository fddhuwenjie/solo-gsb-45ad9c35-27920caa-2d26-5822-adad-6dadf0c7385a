"""JSON HTTP API served by http.server.ThreadingHTTPServer (stdlib only).

Endpoints:
    GET  /health
    GET  /v1/rulesets
    PUT  /v1/rulesets/{name}
    GET  /v1/rulesets/{name}
    POST /v1/rulesets/{name}/revisions
    GET  /v1/rulesets/{name}/revisions
    POST /v1/rulesets/{name}/revisions/{revision}/activate
    POST /v1/assessments
    GET  /v1/assessments/{id}
    POST /v1/assessments/{id}/confirm
    GET  /v1/assessments/{id}/confirmation
    POST /v1/assessments/{id}/fulfillment-check
    POST /v1/assessments/{id}/obligation-schedule
    POST /v1/assessments/{id}/responsibility-plan
    POST /v1/assessments/compare
"""

import json
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .common import ClientError, ConflictError, NotFound, stable_hash
from .compare import compare_assessments
from .fulfillment import UnprocessableError, check_fulfillment
from .responsibility import PartialPlanError, build_plan
from .revisions import REVISION_ID_LEN
from .rules import validate_ruleset
from .schedule import build_schedule
from .solver import assess_normalized, build_confirmation, normalize_request

MAX_BODY_BYTES = 16 * 1024 * 1024
_NAME_RE = re.compile(r"^/v1/rulesets(?:/([A-Za-z0-9._-]{1,64}))?$")
_REVISION_RE = re.compile(
    r"^/v1/rulesets/([A-Za-z0-9._-]{1,64})/revisions"
    r"(?:/([0-9a-f]{%d})(/activate)?)?$" % REVISION_ID_LEN
)
_ASSESS_RE = re.compile(
    r"^/v1/assessments(?:/([0-9a-f]{8,64})"
    r"(/(?:confirm|confirmation|fulfillment-check|obligation-schedule"
    r"|responsibility-plan))?)?$"
)


class Api:
    """Transport-independent core; the HTTP handler is a thin adapter."""

    def __init__(self, store):
        self.store = store

    # -- rulesets ---------------------------------------------------------

    def list_rulesets(self):
        return {"rulesets": self.store.list_rulesets()}

    def put_ruleset(self, name, body):
        rules = validate_ruleset(body)
        created = self.store.put_ruleset(name, rules)
        return {"name": name, "created": created, "ruleset": rules}

    def get_ruleset(self, name):
        history = self.store.list_ruleset_revisions(name)
        if history is None or history["active_revision"] is None:
            # Drafts are invisible to legacy consumers: a name with no
            # published revision behaves like a missing ruleset.
            rules = self.store.get_ruleset(name)
            if rules is None:
                raise NotFound("ruleset %r not found" % name)
            return {"name": name, "ruleset": rules}
        active = next(r for r in history["revisions"] if r["active"])
        return {
            "name": name,
            "ruleset": active["ruleset"],
            "active_revision": history["active_revision"],
        }

    # -- immutable revisions ----------------------------------------------

    def create_revision(self, name, body):
        if not isinstance(body, dict):
            raise ClientError("request body must be a JSON object")
        if "rules" not in body:
            raise ClientError("revision request requires a complete 'rules' object")
        rules = validate_ruleset(body["rules"])
        if "base_revision" not in body:
            raise ClientError(
                "revision request requires 'base_revision'"
                " (null when creating the ruleset's first revision)"
            )
        base_revision = body["base_revision"]
        if base_revision is not None and not isinstance(base_revision, str):
            raise ClientError("base_revision must be a string or null")
        rev = self.store.create_ruleset_revision(name, rules, base_revision)
        rev["ruleset"] = rules
        return rev

    def activate_revision(self, name, revision, body):
        if not isinstance(body, dict):
            raise ClientError("request body must be a JSON object")
        if "expected_active_revision" not in body:
            raise ClientError(
                "activation requires 'expected_active_revision'"
                " (null when publishing the ruleset's first revision)"
            )
        expected = body["expected_active_revision"]
        if expected is not None and not (
            isinstance(expected, str) and expected
        ):
            raise ClientError(
                "'expected_active_revision' must be a non-empty string or null"
            )
        return self.store.activate_ruleset_revision(name, revision, expected)

    def list_revisions(self, name):
        history = self.store.list_ruleset_revisions(name)
        if history is None:
            raise NotFound("ruleset %r not found" % name)
        return history

    # -- assessments ------------------------------------------------------

    def create_assessment(self, body):
        if not isinstance(body, dict):
            raise ClientError("request body must be a JSON object")
        if body.get("rules") is not None:
            rules = validate_ruleset(body["rules"])
            ruleset_name = None
        else:
            ruleset_name = body.get("ruleset", "default")
            if not isinstance(ruleset_name, str) or not ruleset_name:
                raise ClientError("ruleset must be a non-empty string")

        norm = normalize_request(body)

        if ruleset_name is None:
            # id is a pure function of (normalized request, rules) -> idempotent
            # and independent of the input ordering of components/dependencies.
            aid = stable_hash({"request": norm, "rules": rules})[:32]
            existing = self.store.get_assessment(aid)
            if existing is not None:
                return {"id": aid, "created": False, "result": existing["result"]}
            result = assess_normalized(norm, rules)
            # Persist the exact normalized rules snapshot alongside the name:
            # the assessment stays replayable after the same-named ruleset is
            # overwritten and for originally inline-rule requests.
            self.store.put_assessment(aid, ruleset_name, rules, norm, result)
            return {"id": aid, "created": True, "result": result}

        # Named ruleset: read the active revision at transaction start,
        # evaluate against it and record that revision id, all atomically.
        out = self.store.create_named_assessment(ruleset_name, norm)
        return {
            "id": out["id"],
            "created": out["created"],
            "result": out["result"],
            "ruleset_revision": out["revision"],
        }

    def get_assessment(self, aid):
        row = self.store.get_assessment(aid)
        if row is None:
            raise NotFound("assessment %r not found" % aid)
        return row

    # -- confirmation -----------------------------------------------------

    def confirm(self, aid):
        row = self.store.get_assessment(aid)
        if row is None:
            raise NotFound("assessment %r not found" % aid)
        existing = self.store.get_confirmation(aid)
        if existing is not None:
            return existing
        result = row["result"]
        if result.get("status") != "ok":
            raise ConflictError(
                "assessment status is %r; no release conclusion available"
                % result.get("status")
            )
        # A confirmation is only minted when the exact rules the assessment
        # ran with are known.  Newer rows always carry the pinned snapshot;
        # pre-upgrade rows are accepted only when their rules can be verified
        # and restored (see Store.recover_legacy_rules).  A deleted/overwritten
        # ruleset or a legacy inline-rule request can never be reconstructed --
        # refuse rather than fabricate a replay that may not reproduce.
        rules = row["rules"]
        if rules is None:
            rules = self.store.recover_legacy_rules(aid)
        if rules is None:
            if not row["ruleset"]:
                reason = (
                    "the assessment used inline rules that were never persisted; "
                    "re-submit the assessment to bind a rules snapshot"
                )
            else:
                reason = (
                    "the original ruleset %r can no longer be verified "
                    "(ruleset deleted or overwritten since the assessment)"
                    % row["ruleset"]
                )
            raise ConflictError(
                "cannot confirm assessment %r: %s" % (aid, reason)
            )
        conf = build_confirmation(row["request"], result, rules, aid, row["ruleset"])
        self.store.put_confirmation(aid, conf)
        return conf

    def get_confirmation(self, aid):
        conf = self.store.get_confirmation(aid)
        if conf is None:
            raise NotFound("confirmation for assessment %r not found" % aid)
        return conf

    # -- fulfillment ------------------------------------------------------

    def fulfillment_check(self, aid, body):
        row = self.store.get_assessment(aid)
        if row is None:
            raise NotFound("assessment %r not found" % aid)
        result = row["result"]
        if result.get("status") != "ok":
            # No release conclusion exists for a non-releasable assessment,
            # so no fulfillment conclusion may be produced either.
            raise ConflictError(
                "assessment status is %r; fulfillment checks require status 'ok'"
                % result.get("status")
            )
        if not isinstance(body, dict):
            raise ClientError("request body must be a JSON object")
        return check_fulfillment(body, result)

    # -- obligation schedule -----------------------------------------------

    def obligation_schedule(self, aid, body):
        row = self.store.get_assessment(aid)
        if row is None:
            raise NotFound("assessment %r not found" % aid)
        result = row["result"]
        if result.get("status") != "ok":
            # No release conclusion exists for a non-releasable assessment,
            # so no obligation schedule may be produced either.
            raise ConflictError(
                "assessment status is %r; obligation schedules require status 'ok'"
                % result.get("status")
            )
        if not isinstance(body, dict):
            raise ClientError("request body must be a JSON object")
        return build_schedule(body, result, aid)

    # -- responsibility plan -----------------------------------------------

    def responsibility_plan(self, aid, body):
        row = self.store.get_assessment(aid)
        if row is None:
            raise NotFound("assessment %r not found" % aid)
        result = row["result"]
        if result.get("status") != "ok":
            # No release conclusion exists for a non-releasable assessment,
            # so no responsibility plan may be produced either.
            raise ConflictError(
                "assessment status is %r; responsibility plans require status 'ok'"
                % result.get("status")
            )
        if not isinstance(body, dict):
            raise ClientError("request body must be a JSON object")
        return build_plan(body, result, aid)

    # -- comparison -----------------------------------------------------

    def compare(self, body):
        if not isinstance(body, dict):
            raise ClientError("request body must be a JSON object")
        baseline_id = body.get("baseline_id")
        target_id = body.get("target_id")
        if not isinstance(baseline_id, str) or not baseline_id:
            raise ClientError("baseline_id must be a non-empty string")
        if not isinstance(target_id, str) or not target_id:
            raise ClientError("target_id must be a non-empty string")
        baseline = self._assessment_with_rules(baseline_id)
        target = self._assessment_with_rules(target_id)
        return compare_assessments(baseline, target)

    def _assessment_with_rules(self, aid):
        """Fetch an assessment row, refusing ones without provable rules.

        Pre-upgrade rows are accepted only when their rules can be
        verified and restored (the same rule confirmations follow); a
        deleted/overwritten ruleset or a legacy inline-rule request can
        never be reconstructed -- refuse rather than comparing against
        guessed rules.
        """
        row = self.store.get_assessment(aid)
        if row is None:
            raise NotFound("assessment %r not found" % aid)
        if row["rules"] is None:
            row["rules"] = self.store.recover_legacy_rules(aid)
        if row["rules"] is None:
            if not row["ruleset"]:
                reason = (
                    "the assessment used inline rules that were never persisted; "
                    "re-submit the assessment to bind a rules snapshot"
                )
            else:
                reason = (
                    "the original ruleset %r can no longer be verified "
                    "(ruleset deleted or overwritten since the assessment)"
                    % row["ruleset"]
                )
            raise ConflictError("cannot compare assessment %r: %s" % (aid, reason))
        return row


def make_server(host, port, store):
    api = Api(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "LicenseObligationAPI/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # -- helpers ------------------------------------------------------

        def _json(self, code, obj):
            body = json.dumps(obj, sort_keys=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = self.headers.get("Content-Length")
            if length is None:
                raise ClientError("missing Content-Length header")
            try:
                size = int(length)
            except ValueError:
                raise ClientError("invalid Content-Length header")
            if size > MAX_BODY_BYTES:
                raise ClientError("request body too large")
            raw = self.rfile.read(size)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ClientError("request body is not valid JSON")

        # -- routing ------------------------------------------------------

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def _dispatch(self, method):
            path = urlparse(self.path).path
            try:
                if method == "GET" and path == "/health":
                    return self._json(200, {"status": "ok"})
                if method == "GET" and path == "/":
                    return self._json(
                        200,
                        {
                            "service": "license-obligation-api",
                            "endpoints": [
                                "GET  /health",
                                "GET  /v1/rulesets",
                                "PUT  /v1/rulesets/{name}",
                                "GET  /v1/rulesets/{name}",
                                "POST /v1/rulesets/{name}/revisions",
                                "GET  /v1/rulesets/{name}/revisions",
                                "POST /v1/rulesets/{name}/revisions/{revision}/activate",
                                "POST /v1/assessments",
                                "GET  /v1/assessments/{id}",
                                "POST /v1/assessments/{id}/confirm",
                                "GET  /v1/assessments/{id}/confirmation",
                                "POST /v1/assessments/{id}/fulfillment-check",
                                "POST /v1/assessments/{id}/obligation-schedule",
                                "POST /v1/assessments/{id}/responsibility-plan",
                                "POST /v1/assessments/compare",
                            ],
                        },
                    )

                if method == "POST" and path == "/v1/assessments/compare":
                    return self._json(200, api.compare(self._body()))

                m = _REVISION_RE.match(path)
                if m:
                    rname, rev, activate = m.group(1), m.group(2), m.group(3)
                    if method == "POST" and rev is None:
                        return self._json(200, api.create_revision(rname, self._body()))
                    if method == "GET" and rev is None:
                        return self._json(200, api.list_revisions(rname))
                    if method == "POST" and rev is not None and activate == "/activate":
                        return self._json(
                            200, api.activate_revision(rname, rev, self._body())
                        )

                m = _NAME_RE.match(path)
                if m:
                    name = m.group(1)
                    if method == "GET" and name is None:
                        return self._json(200, api.list_rulesets())
                    if method == "GET" and name is not None:
                        return self._json(200, api.get_ruleset(name))
                    if method == "PUT" and name is not None:
                        return self._json(200, api.put_ruleset(name, self._body()))

                m = _ASSESS_RE.match(path)
                if m:
                    aid, sub = m.group(1), m.group(2)
                    if method == "POST" and aid is None:
                        return self._json(200, api.create_assessment(self._body()))
                    if method == "GET" and aid is not None and sub is None:
                        return self._json(200, api.get_assessment(aid))
                    if method == "POST" and aid is not None and sub == "/confirm":
                        return self._json(200, api.confirm(aid))
                    if method == "GET" and aid is not None and sub == "/confirmation":
                        return self._json(200, api.get_confirmation(aid))
                    if method == "POST" and aid is not None and sub == "/fulfillment-check":
                        return self._json(200, api.fulfillment_check(aid, self._body()))
                    if method == "POST" and aid is not None and sub == "/obligation-schedule":
                        return self._json(200, api.obligation_schedule(aid, self._body()))
                    if method == "POST" and aid is not None and sub == "/responsibility-plan":
                        return self._json(200, api.responsibility_plan(aid, self._body()))

                raise NotFound("no route for %s %s" % (method, path))
            except UnprocessableError as exc:
                return self._json(422, {"error": str(exc)})
            except PartialPlanError as exc:
                return self._json(409, exc.payload)
            except ClientError as exc:
                return self._json(400, {"error": str(exc)})
            except NotFound as exc:
                return self._json(404, {"error": str(exc)})
            except ConflictError as exc:
                payload = {"error": str(exc)}
                if exc.details:
                    payload.update(exc.details)
                return self._json(409, payload)
            except Exception:  # pragma: no cover - defensive
                traceback.print_exc()
                return self._json(500, {"error": "internal error"})

    return ThreadingHTTPServer((host, port), Handler)
