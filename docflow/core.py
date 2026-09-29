"""Core DocFlow logic: storing document requests and moving them through stages.

Kept free of Streamlit so it can be tested directly against SQLite.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STAGES = ["received", "picked_up", "in_preparation", "approved", "delivered"]

STAGE_LABELS = {
    "received": "Received",
    "picked_up": "Picked up",
    "in_preparation": "In preparation",
    "approved": "Approved",
    "delivered": "Delivered",
}

DEFAULT_TIMEOUT_MINUTES = 2

_STAGE_COLUMNS = ", ".join(f"{stage}_at TEXT" for stage in STAGES)

_SCHEMA = [
    f"""
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        requester TEXT NOT NULL,
        owner TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        stage TEXT NOT NULL,
        {_STAGE_COLUMNS}
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient TEXT NOT NULL,
        request_id INTEGER NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS people (
        name TEXT PRIMARY KEY COLLATE NOCASE
    )
    """,
]

# Columns added after the first release; added to existing databases on connect.
_ADDED_REQUEST_COLUMNS = {
    "backup": "TEXT NOT NULL DEFAULT ''",
    "reassigned_from": "TEXT",
    "reassigned_at": "TEXT",
    "delivered_by": "TEXT",
}


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
    backup: str = ""
    reassigned_from: str = None
    reassigned_at: str = None
    delivered_by: str = None

    @property
    def is_complete(self):
        return self.stage == STAGES[-1]


@dataclass
class Notification:
    id: int
    recipient: str
    request_id: int
    message: str
    created_at: str
    is_read: bool


def _now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.isoformat(timespec="seconds")


def _stamp(now):
    """Accept a datetime or a preformatted string; default to the current time."""
    if now is None:
        return _iso(_now())
    return _iso(now) if isinstance(now, datetime) else now


def connect(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn):
    for statement in _SCHEMA:
        conn.execute(statement)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    for column, definition in _ADDED_REQUEST_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {column} {definition}")
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
        backup=row["backup"],
        reassigned_from=row["reassigned_from"],
        reassigned_at=row["reassigned_at"],
        delivered_by=row["delivered_by"],
    )


def create_request(conn, title, requester, owner, details="", backup="", now=None):
    """Store a new request in the 'received' stage and return it."""
    title, requester, owner = title.strip(), requester.strip(), owner.strip()
    backup = backup.strip()
    missing = [name for name, value in
               [("title", title), ("requester", requester), ("owner", owner)]
               if not value]
    if missing:
        raise DocFlowError(f"Missing required field(s): {', '.join(missing)}")
    if backup and backup.casefold() == owner.casefold():
        raise DocFlowError("The backup must be someone other than the owner")

    cur = conn.execute(
        "INSERT INTO requests (title, requester, owner, details, backup, stage, received_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title, requester, owner, details.strip(), backup, STAGES[0], _stamp(now)),
    )
    conn.commit()
    return get_request(conn, cur.lastrowid)


def choose_backup(conn, owner, team):
    """Pick a backup for a new request: the least busy team member other than the owner.

    Busy means owning, or being backup on, requests that are not delivered yet; ties
    go to whoever comes first in `team`. Returns '' if nobody else is on the team.
    """
    owner = owner.strip().casefold()
    candidates = [name for name in team if name.casefold() != owner]
    if not candidates:
        return ""
    load = {}
    for request in list_requests(conn):
        if request.is_complete:
            continue
        for name in {request.owner.casefold(), request.backup.casefold()} - {""}:
            load[name] = load.get(name, 0) + 1
    return min(candidates, key=lambda name: load.get(name.casefold(), 0))


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
    """Move a request to its next stage, recording when it got there.

    The final step, delivery, goes through confirm_delivery instead.
    """
    request = get_request(conn, request_id)
    target = next_stage(request.stage)
    if target is None:
        raise DocFlowError(f"Request {request_id} is already delivered")
    if target == STAGES[-1]:
        raise DocFlowError("Delivery must be confirmed by the preparer")

    conn.execute(
        f"UPDATE requests SET stage = ?, {target}_at = ? WHERE id = ?",
        (target, _stamp(now), request_id),
    )
    conn.commit()
    return get_request(conn, request_id)


def can_confirm_delivery(request, person):
    """True if `person` is the preparer (current owner) of an approved request."""
    return (request.stage == "approved" and bool(person)
            and person.strip().casefold() == request.owner.casefold())


def confirm_delivery(conn, request_id, confirmed_by, now=None):
    """Mark an approved request as delivered to the client and notify the requester.

    Only the preparer, meaning the request's current owner, may confirm.
    """
    request = get_request(conn, request_id)
    if request.stage != "approved":
        raise DocFlowError(
            f"Request {request_id} must be approved before delivery can be confirmed")
    if not can_confirm_delivery(request, confirmed_by):
        raise DocFlowError(
            f"Only the preparer ({request.owner}) can confirm delivery")

    delivered_at = _stamp(now)
    cur = conn.execute(
        "UPDATE requests SET stage = 'delivered', delivered_at = ?, delivered_by = ?"
        " WHERE id = ? AND stage = 'approved' AND owner = ?",
        (delivered_at, request.owner, request_id, request.owner),
    )
    if cur.rowcount != 1:
        conn.commit()
        raise DocFlowError(
            f"Request {request_id} changed before delivery could be confirmed")
    _notify(conn, request.requester, request_id, delivered_at,
            f"Your request #{request_id} \"{request.title}\" has been delivered "
            f"(confirmed by {request.owner}).")
    conn.commit()
    return get_request(conn, request_id)


def requests_by_stage(conn):
    """Group all requests by their current stage, for the status board."""
    board = {stage: [] for stage in STAGES}
    for request in list_requests(conn):
        board[request.stage].append(request)
    return board


# --- Reassignment when the owner does not act -------------------------------

def get_timeout_minutes(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = 'timeout_minutes'").fetchone()
    return int(row["value"]) if row else DEFAULT_TIMEOUT_MINUTES


def set_timeout_minutes(conn, minutes):
    minutes = int(minutes)
    if minutes < 1:
        raise DocFlowError("The timeout must be at least 1 minute")
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('timeout_minutes', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(minutes),),
    )
    conn.commit()


def reassign_deadline(request, timeout_minutes):
    """When the request passes to its backup if the owner has not acted.

    The clock starts when the request entered its current stage. Returns None
    if it will never be reassigned: no backup, already reassigned, or delivered.
    """
    if not request.backup or request.reassigned_at or request.is_complete:
        return None
    last_action = datetime.fromisoformat(request.timestamps[request.stage])
    return last_action + timedelta(minutes=timeout_minutes)


def reassign_overdue(conn, now=None):
    """Hand every overdue request to its backup and notify both people.

    Returns the reassigned requests. Safe to call from several sessions at
    once: each request is only reassigned (and notified about) once.
    """
    now = now or _now()
    timeout = get_timeout_minutes(conn)
    reassigned = []
    for request in list_requests(conn):
        deadline = reassign_deadline(request, timeout)
        if deadline is None or now < deadline:
            continue
        cur = conn.execute(
            "UPDATE requests SET owner = backup, reassigned_from = owner, reassigned_at = ?"
            " WHERE id = ? AND reassigned_at IS NULL",
            (_iso(now), request.id),
        )
        if cur.rowcount != 1:
            continue  # another session got there first
        stage = STAGE_LABELS[request.stage].lower()
        _notify(conn, request.owner, request.id, now,
                f"Request #{request.id} \"{request.title}\" was reassigned to "
                f"{request.backup} because it had no action for {timeout} minute(s) "
                f"while {stage}.")
        _notify(conn, request.backup, request.id, now,
                f"You are now the owner of request #{request.id} \"{request.title}\" "
                f"(reassigned from {request.owner}, currently {stage}).")
        conn.commit()
        reassigned.append(get_request(conn, request.id))
    return reassigned


# --- In-app notifications ---------------------------------------------------

def _notify(conn, recipient, request_id, now, message):
    conn.execute(
        "INSERT INTO notifications (recipient, request_id, message, created_at)"
        " VALUES (?, ?, ?, ?)",
        (recipient, request_id, message, _stamp(now)),
    )


def list_notifications(conn, recipient):
    rows = conn.execute(
        "SELECT * FROM notifications WHERE recipient = ? COLLATE NOCASE ORDER BY id DESC",
        (recipient,),
    ).fetchall()
    return [
        Notification(row["id"], row["recipient"], row["request_id"], row["message"],
                     row["created_at"], bool(row["is_read"]))
        for row in rows
    ]


def mark_notifications_read(conn, recipient):
    conn.execute(
        "UPDATE notifications SET is_read = 1 WHERE recipient = ? COLLATE NOCASE",
        (recipient,),
    )
    conn.commit()


def add_person(conn, name):
    """Remember a name so it is offered in the 'You are' picker from now on."""
    name = name.strip()
    if not name:
        raise DocFlowError("A name is required")
    conn.execute("INSERT OR IGNORE INTO people (name) VALUES (?)", (name,))
    conn.commit()


def people(conn):
    """Saved names plus requesters, owners and backups on any request, for the name pickers.

    Names that differ only in case are listed once, using the saved spelling.
    """
    rows = conn.execute(
        "SELECT name FROM people UNION ALL SELECT requester FROM requests"
        " UNION ALL SELECT owner FROM requests"
        " UNION ALL SELECT backup FROM requests UNION ALL SELECT reassigned_from FROM requests"
    ).fetchall()
    names = {}
    for row in rows:
        if row["name"]:
            names.setdefault(row["name"].casefold(), row["name"])
    return sorted(names.values(), key=str.casefold)
