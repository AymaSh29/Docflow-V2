import pytest

from docflow.core import (
    STAGES,
    DocFlowError,
    advance_request,
    connect,
    create_request,
    get_request,
    list_requests,
    requests_by_stage,
)


@pytest.fixture
def conn():
    conn = connect(":memory:")
    yield conn
    conn.close()


def test_new_request_starts_received_with_timestamp(conn):
    request = create_request(conn, "Tax letter", "Sam", "Alex", now="2026-01-01T09:00:00+00:00")

    assert request.stage == "received"
    assert request.timestamps["received"] == "2026-01-01T09:00:00+00:00"
    assert all(request.timestamps[stage] is None for stage in STAGES[1:])


def test_fields_are_trimmed(conn):
    request = create_request(conn, "  Tax letter ", " Sam", "Alex  ", details="  urgent ")

    assert (request.title, request.requester, request.owner, request.details) == (
        "Tax letter", "Sam", "Alex", "urgent",
    )


@pytest.mark.parametrize("field", ["title", "requester", "owner"])
def test_required_fields(conn, field):
    values = {"title": "Tax letter", "requester": "Sam", "owner": "Alex"}
    values[field] = "   "

    with pytest.raises(DocFlowError, match=field):
        create_request(conn, **values)
    assert list_requests(conn) == []


def test_advance_walks_through_every_stage_recording_each_time(conn):
    request = create_request(conn, "Contract", "Sam", "Alex", now="t0")

    for i, stage in enumerate(STAGES[1:], start=1):
        request = advance_request(conn, request.id, now=f"t{i}")
        assert request.stage == stage

    assert request.is_complete
    assert request.timestamps == {stage: f"t{i}" for i, stage in enumerate(STAGES)}


def test_cannot_advance_past_delivered(conn):
    request = create_request(conn, "Contract", "Sam", "Alex")
    for _ in STAGES[1:]:
        advance_request(conn, request.id)

    with pytest.raises(DocFlowError, match="already delivered"):
        advance_request(conn, request.id)
    assert get_request(conn, request.id).stage == "delivered"


def test_unknown_request(conn):
    with pytest.raises(DocFlowError):
        advance_request(conn, 999)


def test_board_groups_requests_by_current_stage(conn):
    first = create_request(conn, "A", "Sam", "Alex")
    second = create_request(conn, "B", "Sam", "Jo")
    advance_request(conn, second.id)

    board = requests_by_stage(conn)

    assert list(board) == STAGES
    assert [r.id for r in board["received"]] == [first.id]
    assert [r.id for r in board["picked_up"]] == [second.id]
    assert all(board[stage] == [] for stage in STAGES[2:])


def test_data_persists_across_connections(tmp_path):
    path = tmp_path / "docflow.db"
    conn = connect(path)
    create_request(conn, "Contract", "Sam", "Alex")
    conn.close()

    conn = connect(path)
    assert [r.title for r in list_requests(conn)] == ["Contract"]
    conn.close()
