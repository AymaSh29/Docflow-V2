"""Check DocFlow against a real Turso database: writes, rowcount and surviving a restart.

It writes test requests, so only run it against a throwaway database:

    python scripts/check_turso.py --throwaway

TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are read from the environment or from
.streamlit/secrets.toml.
"""

import os
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docflow.core import (  # noqa: E402
    advance_request,
    confirm_delivery,
    connect,
    create_request,
    list_requests,
    turso_settings,
)

if "--throwaway" not in sys.argv:
    sys.exit("This writes test data. Run it only against a throwaway database, "
             "with --throwaway to confirm.")

secrets_file = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
secrets = tomllib.loads(secrets_file.read_text()) if secrets_file.exists() else {}
url, token = turso_settings(secrets, os.environ)
if not url:
    sys.exit("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are not set.")

title = f"Turso check {datetime.now(timezone.utc).isoformat(timespec='seconds')}"

with tempfile.TemporaryDirectory() as first_disk:
    conn = connect(Path(first_disk) / "replica.db", url, token)
    request = create_request(conn, title, "Check", "Alex")
    for _ in range(3):
        advance_request(conn, request.id)
    confirm_delivery(conn, request.id, "Alex")
    claim = "UPDATE requests SET reassigned_at = ? WHERE id = ? AND reassigned_at IS NULL"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = [conn.execute(claim, (now, request.id)).rowcount for _ in range(2)]
    conn.commit()
    conn.close()
assert counts == [1, 0], f"rowcount should be [1, 0], got {counts}"
print(f"Wrote request #{request.id}; rowcount guards work: {counts}")

# A fresh, empty local disk, as after a restart: everything must come back from Turso.
with tempfile.TemporaryDirectory() as second_disk:
    conn = connect(Path(second_disk) / "replica.db", url, token)
    found = [r for r in list_requests(conn) if r.title == title]
    conn.close()
assert len(found) == 1 and found[0].stage == "delivered", f"after restart: {found}"
print(f"Request #{found[0].id} survived a restart on an empty disk. Turso check passed.")
