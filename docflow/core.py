"""Core DocFlow logic: storing document requests and moving them through stages.

Kept free of Streamlit so it can be tested directly against SQLite.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

STAGES = ["received", "picked_up", "in_preparation", "approved", "delivered"]

STAGE_LABELS = {
    "received": "Received",
    "picked_up": "Picked up",
    "in_preparation": "In preparation",
    "approved": "Approved",
    "delivered": "Delivered",
}

_STAGE_COLUMNS = ", ".join(f"{stage}_at TEXT" for stage in STAGES)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    requester TEXT NOT NULL,
    owner TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL,
    {_STAGE_COLUMNS}
)
"""


class DocFlowError(ValueError):
    """Raised when a request is invalid or cannot move to the next stage."""


@dataclass
class Request:
    id: int
    title: str
    requester: str
    owner: str
    details: str
    stage: str
    timestamps: dict  # stage -> ISO 8601 UTC string, or None if not reached yet

    @property
    def is_complete(self):
        return self.stage == STAGES[-1]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn):
    conn.execute(_SCHEMA)
    conn.commit()


def _row_to_request(row):
    return Request(
        id=row["id"],
        title=row["title"],
        requester=row["requester"],
        owner=row["owner"],
        details=row["details"],
        stage=row["stage"],
        timestamps={stage: row[f"{stage}_at"] for stage in STAGES},
    )


def create_request(conn, title, requester, owner, details="", now=None):
    """Store a new request in the 'received' stage and return it."""
    title, requester, owner = title.strip(), requester.strip(), owner.strip()
    missing = [name for name, value in
               [("title", title), ("requester", requester), ("owner", owner)]
               if not value]
    if missing:
        raise DocFlowError(f"Missing required field(s): {', '.join(missing)}")

    cur = conn.execute(
        "INSERT INTO requests (title, requester, owner, details, stage, received_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (title, requester, owner, details.strip(), STAGES[0], now or _now()),
    )
    conn.commit()
    return get_request(conn, cur.lastrowid)


def get_request(conn, request_id):
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise DocFlowError(f"No request with id {request_id}")
    return _row_to_request(row)


def list_requests(conn):
    rows = conn.execute("SELECT * FROM requests ORDER BY id").fetchall()
    return [_row_to_request(row) for row in rows]


def next_stage(stage):
    """Return the stage after `stage`, or None if it is the last one."""
    index = STAGES.index(stage)
    return STAGES[index + 1] if index + 1 < len(STAGES) else None


def advance_request(conn, request_id, now=None):
    """Move a request to its next stage, recording when it got there."""
    request = get_request(conn, request_id)
    target = next_stage(request.stage)
    if target is None:
        raise DocFlowError(f"Request {request_id} is already delivered")

    conn.execute(
        f"UPDATE requests SET stage = ?, {target}_at = ? WHERE id = ?",
        (target, now or _now(), request_id),
    )
    conn.commit()
    return get_request(conn, request_id)


def requests_by_stage(conn):
    """Group all requests by their current stage, for the status board."""
    board = {stage: [] for stage in STAGES}
    for request in list_requests(conn):
        board[request.stage].append(request)
    return board
