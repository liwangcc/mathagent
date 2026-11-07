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


# Registered only when frontend/dist exists (app.py:752), so they differ between
# a developer machine and a CI checkout that has not built the frontend.
_SPA_ONLY_PATHS = frozenset({"/", "/{full_path:path}", "/{full_path}"})


def observed() -> dict:
    from app import app
    from fastapi.routing import APIRoute

    # Only API routes are the contract. Static mounts differ by environment too,
    # and neither they nor the SPA fallback are what a module split must preserve.
    routes = [
        [
            getattr(route, "path", ""),
            sorted(method.upper() for method in (getattr(route, "methods", None) or [])),
            getattr(getattr(route, "endpoint", None), "__qualname__", ""),
        ]
        for route in app.routes
        if isinstance(route, APIRoute) and route.path not in _SPA_ONLY_PATHS
    ]
    # The app memoises its OpenAPI document; drop the memo so the document
    # always describes the routes this process actually has.
    app.openapi_schema = None
    spec = app.openapi()
    paths = {path: ops for path, ops in spec["paths"].items() if path not in _SPA_ONLY_PATHS}
    document = {"paths": paths, "components": spec.get("components", {})}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "routes": routes,
        "openapi_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "openapi_paths": {path: sorted(ops) for path, ops in paths.items()},
    }