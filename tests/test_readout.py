from datetime import timedelta

import pytest

from docflow.core import (
    advance_request,
    confirm_delivery,
    connect,
    create_request,
    list_requests,
    processing_readout,
)


@pytest.fixture
def conn():
    conn = connect(":memory:")
    yield conn
    conn.close()


def deliver(conn, minutes):
    """Create a request and walk it to delivered, spending `minutes[i]` before stage i+1."""
    at = 0
    request = create_request(conn, "Contract", "Sam", "Alex", now="2026-01-01T09:00:00+00:00")
    for step in minutes[:-1]:
        at += step
        advance_request(conn, request.id, now=f"2026-01-01T{9 + at // 60:02d}:{at % 60:02d}:00+00:00")
    at += minutes[-1]
    confirm_delivery(conn, request.id, "Alex",
                     now=f"2026-01-01T{9 + at // 60:02d}:{at % 60:02d}:00+00:00")


def test_nothing_delivered_gives_no_readout(conn):
    create_request(conn, "Contract", "Sam", "Alex")

    assert processing_readout(list_requests(conn)) is None


def test_splits_processing_into_waiting_and_preparation(conn):
    # received→picked up 5, →in preparation 10, →approved 30, →delivered 15
    deliver(conn, [5, 10, 30, 15])

    readout = processing_readout(list_requests(conn))

    assert readout.delivered == 1
    assert readout.median_total == timedelta(minutes=60)
    assert readout.median_preparation == timedelta(minutes=30)
    assert readout.median_waiting == timedelta(minutes=30)
    assert readout.waiting_share == 0.5


def test_medians_and_share_across_requests(conn):
    deliver(conn, [1, 1, 8, 0])     # total 10, prep 8, wait 2
    deliver(conn, [10, 10, 20, 0])  # total 40, prep 20, wait 20
    deliver(conn, [5, 5, 10, 10])   # total 30, prep 10, wait 20
    create_request(conn, "Still open", "Sam", "Alex")

    readout = processing_readout(list_requests(conn))

    assert readout.delivered == 3
    assert readout.median_total == timedelta(minutes=30)
    assert readout.median_preparation == timedelta(minutes=10)
    assert readout.median_waiting == timedelta(minutes=20)
    assert readout.waiting_share == pytest.approx(42 / 80)
