#!/usr/bin/env python3
"""Verify that no token ever reaches a log handler.

Emits log records containing a fake Google token, an access_token pair and a
Bearer header through both the root logger and a named child logger (which is the
case a root-only filter would miss), then asserts every captured line is redacted.
"""

import io
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from security import SensitiveDataFilter, setup_secure_logging  # noqa: E402

FAKE = {
    "google": "ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "access": "access_token: ABCDEFGHIJKLMNOP1234567890",
    "refresh": "refresh_token=1//0eXaMpLeReFrEsHtOkEn0123456789",
    "bearer": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "insta": "instagram_token: IGQVJYabc123def456",
}

stream = io.StringIO()
logging.basicConfig(
    level=logging.INFO,
    stream=stream,
    format="%(name)s %(message)s",
    force=True,
)
setup_secure_logging()

root = logging.getLogger()
child = logging.getLogger("platform_upload")  # propagates to root
for key, value in FAKE.items():
    root.info(f"root {key}: {value}")
    child.info(f"child {key}: {value}")

output = stream.getvalue()
print(output)
print("-" * 60)
failures = []
for key, secret in FAKE.items():
    if secret in output:
        failures.append(key)
if "[REDACTED]" not in output:
    failures.append("no [REDACTED] marker emitted")
if failures:
    print("FAIL: leaked or missing:", failures)
    sys.exit(1)
print("PASS: all 5 token shapes redacted across root and child loggers")
assert isinstance(SensitiveDataFilter(), logging.Filter)