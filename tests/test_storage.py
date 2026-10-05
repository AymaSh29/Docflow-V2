import sqlite3
from datetime import datetime, timedelta, timezone

import libsql
import pytest

from docflow.core import (
    DocFlowError,
    LibsqlConnection,
    add_person,
    advance_request,
    confirm_delivery,
    connect,
    create_request,
    get_timeout_minutes,
    init_db,
    list_notifications,
    list_requests,
    mark_notifications_read,
    people,
    processing_readout,
    reassign_overdue,
    set_timeout_minutes,
    turso_settings,
)

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
URL, TOKEN = "libsql://docflow-test.turso.io", "token"


def minutes(n):
    return T0 + timedelta(minutes=n)


def open_sqlite(path):
    return connect(path)


def open_libsql(path):
    """The connection the app uses with Turso, on a plain local file so no network is needed."""
    conn = LibsqlConnection(libsql.connect(str(path)))
    init_db(conn)
    return conn


# --- Choosing storage ---------------------------------------------------------

def test_without_turso_settings_connect_uses_local_sqlite(tmp_path):
    conn = connect(tmp_path / "docflow.db")
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_no_turso_settings_means_local_storage():
    assert turso_settings({}, {}) == (None, None)


def test_turso_settings_from_secrets():
    secrets = {"TURSO_DATABASE_URL": URL, "TURSO_AUTH_TOKEN": TOKEN}
    assert turso_settings(secrets, {}) == (URL, TOKEN)


def test_turso_settings_fall_back_to_environment():
    environ = {"TURSO_DATABASE_URL": f" {URL} ", "TURSO_AUTH_TOKEN": TOKEN}
    assert turso_settings({}, environ) == (URL, TOKEN)


def test_secrets_take_priority_and_blanks_count_as_unset():
    secrets = {"TURSO_DATABASE_URL": URL, "TURSO_AUTH_TOKEN": "  "}
    environ = {"TURSO_DATABASE_URL": "libsql://other.turso.io", "TURSO_AUTH_TOKEN": TOKEN}
    assert turso_settings(secrets, environ) == (URL, TOKEN)


@pytest.mark.parametrize("settings", [
    {"TURSO_DATABASE_URL": URL},
    {"TURSO_AUTH_TOKEN": TOKEN},
])
def test_only_one_turso_setting_is_an_error(settings):
    with pytest.raises(DocFlowError, match="both"):
        turso_settings(settings, {})


def test_turso_connect_needs_a_token(tmp_path):
    with pytest.raises(DocFlowError, match="token"):
        connect(tmp_path / "replica.db", sync_url=URL)


# --- The libsql connection behaves like the sqlite3 one -------------------------

def test_libsql_rows_read_by_name_and_position(tmp_path):
    conn = open_libsql(tmp_path / "docflow.db")
    create_request(conn, "Contract", "Sam", "Alex")

    row = conn.execute("SELECT id, title FROM requests").fetchone()
    assert (row["id"], row["title"]) == (row[0], row[1]) == (1, "Contract")
    assert conn.execute("SELECT * FROM requests WHERE id = 99").fetchone() is None
    assert "backup" in {column[1] for column in conn.execute("PRAGMA table_info(requests)")}
    conn.close()


def test_libsql_lastrowid_and_rowcount(tmp_path):
    conn = open_libsql(tmp_path / "docflow.db")
    assert [create_request(conn, title, "Sam", "Alex").id for title in "AB"] == [1, 2]

    claim = "UPDATE requests SET reassigned_at = 'now' WHERE id = 1 AND reassigned_at IS NULL"
    assert conn.execute(claim).rowcount == 1
    assert conn.execute(claim).rowcount == 0
    conn.close()


# --- Data survives a restart ----------------------------------------------------

def snapshot(conn):
    return {
        "requests": list_requests(conn),
        "notifications": {name: list_notifications(conn, name) for name in people(conn)},
        "people": people(conn),
        "timeout": get_timeout_minutes(conn),
        "readout": processing_readout(list_requests(conn)),
    }


@pytest.mark.parametrize("open_db", [open_sqlite, open_libsql], ids=["sqlite", "libsql"])
def test_data_survives_restart(tmp_path, open_db):
    path = tmp_path / "docflow.db"
    conn = open_db(path)
    set_timeout_minutes(conn, 5)
    add_person(conn, "Maria")
    delivered = create_request(conn, "Contract", "Sam", "Alex", "urgent", "Kostas", now=T0)
    for n in (1, 2, 3):
        advance_request(conn, delivered.id, now=minutes(n))
    confirm_delivery(conn, delivered.id, "Alex", now=minutes(4))
    overdue = create_request(conn, "Invoice", "Lee", "Elina", backup="Ayma", now=T0)
    reassign_overdue(conn, now=minutes(10))
    mark_notifications_read(conn, "Sam")
    before = snapshot(conn)
    conn.close()

    conn = open_db(path)  # the app starting again on the same storage
    after = snapshot(conn)

    assert after == before
    contract, invoice = after["requests"]
    assert (contract.stage, contract.delivered_by, contract.details) == ("delivered", "Alex", "urgent")
    assert all(contract.timestamps.values())
    assert (invoice.owner, invoice.reassigned_from) == ("Ayma", "Elina")
    assert [n.is_read for n in after["notifications"]["Sam"]] == [True]
    assert [n.is_read for n in after["notifications"]["Ayma"]] == [False]
    assert "Maria" in after["people"]
    assert after["timeout"] == 5
    assert after["readout"].median_total == timedelta(minutes=4)
    assert create_request(conn, "Letter", "Sam", "Alex").id == overdue.id + 1
    conn.close()
