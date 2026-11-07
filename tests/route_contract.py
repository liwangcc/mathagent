"""The app's route table and OpenAPI document, frozen as a contract.

Splitting a module must not move a route: not its path, not its methods, not the
function that serves it, and not the shape of the OpenAPI document the frontend
and the desktop client are built against. This file records what the running app
answers today; tests/test_route_table_identity.py compares the live app with it.

Only run this when a contract change is intended (a route added, removed,
renamed, or moved between verbs) and write that reason in the commit message:

    python scripts/freeze_route_table.py

Shape:
  routes         ordered [path, methods, endpoint qualname] as app.routes lists them
  openapi_sha256 sha256 of the canonical (sorted, compact) OpenAPI JSON
  openapi_paths  path -> sorted operations, for a readable diff when it changes
"""

import hashlib
import json


def observed() -> dict:
    from app import app

    routes = [
        [
            getattr(route, "path", ""),
            sorted(method.upper() for method in (getattr(route, "methods", None) or [])),
            getattr(getattr(route, "endpoint", None), "__qualname__", ""),
        ]
        for route in app.routes
    ]
    # The app memoises its OpenAPI document; drop the memo so the document
    # always describes the routes this process actually has.
    app.openapi_schema = None
    spec = app.openapi()
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "routes": routes,
        "openapi_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "openapi_paths": {path: sorted(ops) for path, ops in spec["paths"].items()},
    }