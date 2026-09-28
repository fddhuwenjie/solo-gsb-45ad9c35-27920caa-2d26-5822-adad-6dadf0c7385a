#!/usr/bin/env python3
"""Entry point: seed the default ruleset and serve the JSON API.

Configuration via environment variables:
    HOST    bind address (default 0.0.0.0)
    PORT    listen port   (default 8080)
    APP_DB  SQLite path   (default ./data/app.db)
"""

import os
import sys

from app.rules import DEFAULT_RULESET, validate_ruleset
from app.server import make_server
from app.store import Store


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    db_path = os.environ.get("APP_DB", os.path.join("data", "app.db"))

    store = Store(db_path)
    if store.get_ruleset("default") is None:
        store.put_ruleset("default", validate_ruleset(DEFAULT_RULESET))

    server = make_server(host, port, store)
    print(
        "license-obligation-api listening on http://%s:%d (db: %s)"
        % (host, port, db_path),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
