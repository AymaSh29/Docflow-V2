from datetime import datetime, timedelta, timezone

import pytest

from docflow.core import (
    DEFAULT_TIMEOUT_MINUTES,
    DocFlowError,
    advance_request,
    confirm_delivery,
    connect,
    create_request,
    get_request,
    get_timeout_minutes,
    list_notifications,
    mark_notifications_read,
    people,
    reassign_overdue,
    set_timeout_minutes,
)

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


def minutes(n):
    return T0 + timedelta(minutes=n)


@pytest.fixture
def conn():
    conn = connect(":memory:")
    set_timeout_minutes(conn, 5)
    yield conn
    conn.close()


def test_timeout_defaults_and_is_configurable():
    conn = connect(":memory:")
    assert get_timeout_minutes(conn) == DEFAULT_TIMEOUT_MINUTES
    set_timeout_minutes(conn, 7)
    assert get_timeout_minutes(conn) == 7
    with pytest.raises(DocFlowError):
        set_timeout_minutes(conn, 0)


def test_backup_cannot_be_the_owner(conn):
    with pytest.raises(DocFlowError, match="backup"):
        create_request(conn, "Contract", "Sam", "Alex", backup=" alex ")


def test_not_reassigned_before_timeout(conn):
    request = create_request(conn, "Contract", "Sam", "Alex", backup="Bo", now=T0)

    assert reassign_overdue(conn, now=minutes(4.9)) == []
    assert get_request(conn, request.id).owner == "Alex"


def test_reassigned_to_backup_after_timeout_and_both_notified(conn):
    request = create_request(conn, "Contract", "Sam", "Alex", backup="Bo", now=T0)

    reassigned = reassign_overdue(conn, now=minutes(5))

    assert [r.id for r in reassigned] == [request.id]
    request = get_request(conn, request.id)
    assert (request.owner, request.reassigned_from) == ("Bo", "Alex")
    assert request.reassigned_at == minutes(5).isoformat(timespec="seconds")
    assert request.stage == "received"  # reassignment does not move the stage

    [to_owner] = list_notifications(conn, "Alex")
    [to_backup] = list_notifications(conn, "Bo")
    assert "reassigned to Bo" in to_owner.message
    assert "You are now the owner" in to_backup.message
    assert not to_owner.is_read and not to_backup.is_read


def test_acting_resets_the_clock(conn):
    request = create_request(conn, "Contract", "Sam", "Alex", backup="Bo", now=T0)
    advance_request(conn, request.id, now=minutes(4))

    assert reassign_overdue(conn, now=minutes(8)) == []
    assert len(reassign_overdue(conn, now=minutes(9))) == 1


def test_reassigned_only_once_with_no_duplicate_notifications(conn):
    request = create_request(conn, "Contract", "Sam", "Alex", backup="Bo", now=T0)
    reassign_overdue(conn, now=minutes(5))

    assert reassign_overdue(conn, now=minutes(60)) == []
    assert get_request(conn, request.id).owner == "Bo"
    assert len(list_notifications(conn, "Alex")) == 1
    assert len(list_notifications(conn, "Bo")) == 1


def test_no_backup_or_delivered_means_no_reassignment(conn):
    no_backup = create_request(conn, "A", "Sam", "Alex", now=T0)
    delivered = create_request(conn, "B", "Sam", "Alex", backup="Bo", now=T0)
    for _ in range(3):
        advance_request(conn, delivered.id, now=T0)
    confirm_delivery(conn, delivered.id, "Alex", now=T0)

    assert reassign_overdue(conn, now=minutes(60)) == []
    assert get_request(conn, no_backup.id).owner == "Alex"
    assert get_request(conn, delivered.id).owner == "Alex"


def test_timeout_change_applies_to_waiting_requests(conn):
    create_request(conn, "Contract", "Sam", "Alex", backup="Bo", now=T0)
    assert reassign_overdue(conn, now=minutes(3)) == []

    set_timeout_minutes(conn, 2)
    assert len(reassign_overdue(conn, now=minutes(3))) == 1


def test_notifications_are_per_person_and_can_be_marked_read(conn):
    create_request(conn, "Contract", "Sam", "Alex", backup="Bo", now=T0)
    reassign_overdue(conn, now=minutes(5))

    mark_notifications_read(conn, "bo")  # names match case-insensitively

    assert all(n.is_read for n in list_notifications(conn, "Bo"))
    assert not any(n.is_read for n in list_notifications(conn, "Alex"))
    assert people(conn) == ["Alex", "Bo"]


def test_existing_database_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " requester TEXT NOT NULL, owner TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',"
        " stage TEXT NOT NULL, received_at TEXT, picked_up_at TEXT, in_preparation_at TEXT,"
        " approved_at TEXT, delivered_at TEXT)"
    )
    old.execute("INSERT INTO requests (title, requester, owner, stage, received_at)"
                " VALUES ('Old', 'Sam', 'Alex', 'received', '2026-01-01T09:00:00+00:00')")
    old.commit()
    old.close()

    conn = connect(path)
    request = get_request(conn, 1)
    assert (request.title, request.backup, request.reassigned_at) == ("Old", "", None)
    assert reassign_overdue(conn, now=minutes(60)) == []  # no backup, left alone
    conn.close()
