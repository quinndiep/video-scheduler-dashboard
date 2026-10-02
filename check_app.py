#!/usr/bin/env python3
"""Import the FastAPI app and report its route table.

Verifies the module imports cleanly (circular-import check), prints every
registered route with its HTTP methods, and asserts the OpenAPI schema builds.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import app as app_module  # noqa: E402

app = app_module.app

print(f"App: {app.title} v{app.version}")
print(f"Routes ({len(app.routes)}):")
for route in sorted(app.routes, key=lambda r: getattr(r, "path", "")):
    methods = ",".join(sorted(getattr(route, "methods", []) or []))
    if not getattr(route, "path", "").startswith(("/api", "/health", "/openapi")):
        continue
    print(f"  {methods:<18} {route.path}")

schema = app.openapi()
paths = sorted(schema["paths"])
print(f"\nOpenAPI paths ({len(paths)}): {paths}")

# Key routes the migration must provide.
required = {
    "/health",
    "/api/v1/upload",
    "/api/v1/jobs",
    "/api/v1/jobs/{job_id}",
    "/api/v1/jobs/{job_id}/cancel",
    "/api/v1/jobs/{job_id}/start",
    "/api/v1/drive/videos",
    "/api/v1/drive/folders",
    "/api/v1/accounts",
    "/api/v1/accounts/{account_id}",
    "/api/v1/youtube/channel",
}
missing = sorted(required - set(paths))
if missing:
    print("MISSING ROUTES:", missing)
    sys.exit(1)
print("All required routes present in the OpenAPI schema.")
print(json.dumps(schema["info"], indent=2))