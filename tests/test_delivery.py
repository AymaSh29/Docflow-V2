from datetime import datetime, timedelta, timezone

import pytest

from docflow.core import (
    DocFlowError,
    advance_request,
    can_confirm_delivery,
    confirm_delivery,
    connect,
    create_request,
    get_request,
    list_notifications,
    people,
    reassign_overdue,
    set_timeout_minutes,
)

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    conn = connect(":memory:")
    yield conn
    conn.close()


def approved_request(conn, owner="Alex", backup=""):
    request = create_request(conn, "Contract", "Sam", owner, backup=backup, now=T0)
    for _ in range(3):
        request = advance_request(conn, request.id, now=T0)
    assert request.stage == "approved"
    return request


def test_preparer_confirms_delivery_in_one_step(conn):
    request = approved_request(conn)

    request = confirm_delivery(conn, request.id, "Alex", now="2026-01-02T10:00:00+00:00")

    assert request.stage == "delivered"
    assert request.timestamps["delivered"] == "2026-01-02T10:00:00+00:00"
    assert request.delivered_by == "Alex"


def test_requester_is_notified_of_delivery(conn):
    request = approved_request(conn)

    confirm_delivery(conn, request.id, "Alex", now="2026-01-02T10:00:00+00:00")

    [note] = list_notifications(conn, "sam")  # names match case-insensitively
    assert note.request_id == request.id
    assert note.message == (f"Your request #{request.id} \"Contract\" has been delivered "
                            "(confirmed by Alex).")
    assert note.created_at == "2026-01-02T10:00:00+00:00"
    assert not note.is_read
    assert list_notifications(conn, "Alex") == []
    assert "Sam" in people(conn)


def test_name_match_ignores_case_and_spaces_but_stores_owner_name(conn):
    request = approved_request(conn)

    request = confirm_delivery(conn, request.id, "  alex ")

    assert request.delivered_by == "Alex"


def test_only_the_preparer_can_confirm(conn):
    request = approved_request(conn)

    for person in ["Sam", "Bo", "", None]:
        with pytest.raises(DocFlowError, match="Only the preparer"):
            confirm_delivery(conn, request.id, person)
    assert get_request(conn, request.id).stage == "approved"
    assert list_notifications(conn, "Sam") == []


def test_must_be_approved_first(conn):
    request = create_request(conn, "Contract", "Sam", "Alex")
    advance_request(conn, request.id)  # picked up

    with pytest.raises(DocFlowError, match="must be approved"):
        confirm_delivery(conn, request.id, "Alex")


def test_cannot_confirm_twice(conn):
    request = approved_request(conn)
    confirm_delivery(conn, request.id, "Alex")

    with pytest.raises(DocFlowError, match="must be approved"):
        confirm_delivery(conn, request.id, "Alex")


def test_plain_advance_cannot_deliver(conn):
    request = approved_request(conn)

    with pytest.raises(DocFlowError, match="confirmed by the preparer"):
        advance_request(conn, request.id)
    assert get_request(conn, request.id).stage == "approved"


def test_after_reassignment_the_backup_is_the_preparer(conn):
    set_timeout_minutes(conn, 5)
    request = approved_request(conn, owner="Alex", backup="Bo")
    reassign_overdue(conn, now=T0 + timedelta(minutes=5))

    with pytest.raises(DocFlowError, match=r"Only the preparer \(Bo\)"):
        confirm_delivery(conn, request.id, "Alex")
    assert confirm_delivery(conn, request.id, "Bo").delivered_by == "Bo"


def test_can_confirm_delivery_helper(conn):
    request = approved_request(conn)

    assert can_confirm_delivery(request, "Alex")
    assert not can_confirm_delivery(request, "Sam")
    assert not can_confirm_delivery(request, None)
    received = create_request(conn, "Other", "Sam", "Alex")
    assert not can_confirm_delivery(received, "Alex")


def test_confirm_fails_if_reassigned_in_the_meantime(conn, monkeypatch):
    request = approved_request(conn, owner="Alex", backup="Bo")
    stale = get_request(conn, request.id)
    conn.execute("UPDATE requests SET owner = 'Bo' WHERE id = ?", (request.id,))
    monkeypatch.setattr("docflow.core.get_request", lambda conn, request_id: stale)

    with pytest.raises(DocFlowError, match="changed before delivery"):
        confirm_delivery(conn, request.id, "Alex")
    monkeypatch.undo()
    assert get_request(conn, request.id).stage == "approved"
    assert list_notifications(conn, "Sam") == []
