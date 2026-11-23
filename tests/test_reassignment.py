from datetime import datetime, timedelta, timezone

import pytest

from docflow.core import (
    DEFAULT_TIMEOUT_MINUTES,
    STAGES,
    DocFlowError,
    add_person,
    advance_request,
    assignable_people,
    choose_backup,
    choose_owner,
    confirm_delivery,
    connect,
    create_request,
    get_request,
    get_timeout_minutes,
    is_requester_only,
    list_notifications,
    mark_notifications_read,
    people,
    reassign_overdue,
    set_requester_only,
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


def test_backup_is_least_busy_teammate_other_than_owner(conn):
    team = ["Ayma", "Kostas", "Alex"]
    assert choose_backup(conn, "Ayma", team) == "Kostas"  # tie goes to team order
    assert choose_backup(conn, " ayma ", team) == "Kostas"

    open_request = create_request(conn, "A", "Sam", "Kostas", now=T0)
    assert choose_backup(conn, "Ayma", team) == "Alex"

    create_request(conn, "B", "Sam", "Elina", backup="Alex", now=T0)
    assert choose_backup(conn, "Elina", team) == "Ayma"  # being a backup counts too

    for _ in range(3):
        advance_request(conn, open_request.id, now=T0)
    confirm_delivery(conn, open_request.id, "Kostas", now=T0)
    assert choose_backup(conn, "Ayma", team) == "Kostas"  # delivered work does not count

    assert choose_backup(conn, "Ayma", ["Ayma"]) == ""
    assert choose_backup(conn, "Ayma", []) == ""


def test_owner_is_least_busy_teammate(conn):
    team = ["Ayma", "Kostas", "Alex"]
    assert choose_owner(conn, team) == "Ayma"  # tie goes to team order

    create_request(conn, "A", "Sam", "Ayma", backup="Kostas", now=T0)
    assert choose_owner(conn, team) == "Alex"  # Kostas owns none but backs up one

    create_request(conn, "B", "Sam", "Alex", backup="Ayma", now=T0)
    assert choose_owner(conn, team) == "Kostas"  # owning counts before backing up

    assert choose_owner(conn, []) == ""


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

    [to_owner] = [n for n in list_notifications(conn, "Alex") if "reassigned" in n.message]
    [to_backup] = [n for n in list_notifications(conn, "Bo") if "now the owner" in n.message]
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
    # One note from submitting plus exactly one from the reassignment.
    assert len(list_notifications(conn, "Alex")) == 2
    assert len(list_notifications(conn, "Bo")) == 2


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
    assert people(conn) == ["Alex", "Bo", "Sam"]


def test_added_people_are_saved_and_listed_once(tmp_path):
    path = tmp_path / "people.db"
    conn = connect(path)
    add_person(conn, "  Cam ")
    add_person(conn, "cam")  # same person, different case
    create_request(conn, "Contract", "Sam", "alex", backup="Cam", now=T0)
    add_person(conn, "Alex")
    conn.close()

    conn = connect(path)
    assert people(conn) == ["Alex", "Cam", "Sam"]
    with pytest.raises(DocFlowError):
        add_person(conn, "   ")
    conn.close()


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


# --- Assigning from the saved people ------------------------------------------


def test_assignable_people_is_everyone_saved_except_requester_only(conn):
    for name in ["Ayma", "bo", "Cam", "Sam"]:
        add_person(conn, name)
    set_requester_only(conn, "Sam", True)
    assert assignable_people(conn) == ["Ayma", "bo", "Cam"]
    set_requester_only(conn, " sam ", False)
    assert assignable_people(conn) == ["Ayma", "bo", "Cam", "Sam"]
    assert not is_requester_only(conn, "Sam")


def test_set_requester_only_saves_unknown_names_and_rejects_blank(conn):
    set_requester_only(conn, "Dee", True)
    assert is_requester_only(conn, "Dee")
    assert "Dee" in people(conn)
    assert "Dee" not in assignable_people(conn)
    with pytest.raises(DocFlowError):
        set_requester_only(conn, "  ", True)


def test_people_saved_later_are_assigned_and_requester_only_never_is(conn):
    for name in ["Ayma", "Kostas", "Alex", "Elina"]:
        add_person(conn, name)
    for name in ["Ayma", "Kostas", "Alex", "Elina"]:
        create_request(conn, "Busy", "Sam", name, backup="Ayma" if name != "Ayma" else "Kostas")
    add_person(conn, "Newcomer")
    set_requester_only(conn, "Idle", True)
    pool = assignable_people(conn)
    owner = choose_owner(conn, pool)
    backup = choose_backup(conn, owner, pool)
    assert owner == "Newcomer"  # nobody owns less, and it was not in the old four
    assert backup not in ("Idle", owner)


def test_owner_and_backup_differ_and_small_pools_are_handled(conn):
    add_person(conn, "Ayma")
    add_person(conn, "Bo")
    pool = assignable_people(conn)
    owner = choose_owner(conn, pool)
    assert choose_backup(conn, owner, pool).casefold() != owner.casefold()
    set_requester_only(conn, "Bo", True)
    pool = assignable_people(conn)
    assert pool == ["Ayma"]
    assert choose_backup(conn, choose_owner(conn, pool), pool) == ""
    set_requester_only(conn, "Ayma", True)
    assert choose_owner(conn, assignable_people(conn)) == ""


def test_reassignment_goes_to_a_backup_from_the_pool(conn):
    for name in ["Ayma", "Kostas"]:
        add_person(conn, name)
    set_requester_only(conn, "Sam", True)
    pool = assignable_people(conn)
    owner = choose_owner(conn, pool)
    backup = choose_backup(conn, owner, pool)
    request = create_request(conn, "Contract", "Sam", owner, backup=backup, now=T0)
    reassigned = reassign_overdue(conn, now=minutes(5))
    assert [r.id for r in reassigned] == [request.id]
    assert get_request(conn, request.id).owner == backup != "Sam"


def test_requester_only_column_is_added_to_an_old_people_table(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE people (name TEXT PRIMARY KEY COLLATE NOCASE)")
    raw.execute("INSERT INTO people (name) VALUES ('Old')")
    raw.commit()
    raw.close()
    for _ in range(2):  # connecting again must be harmless
        conn = connect(str(path))
        assert assignable_people(conn) == ["Old"]
        conn.close()


# --- Notifications on submit --------------------------------------------------


def test_owner_and_backup_are_notified_when_a_request_is_submitted(conn):
    request = create_request(conn, "Contract", "Sam", "Alex", backup="Bo", now=T0)

    [to_owner] = list_notifications(conn, "Alex")
    [to_backup] = list_notifications(conn, "bo")  # names match case-insensitively
    assert to_owner.request_id == to_backup.request_id == request.id
    assert to_owner.message == (f"You have been assigned request #{request.id} "
                                "\"Contract\" (requested by Sam).")
    assert to_backup.message.startswith(
        f"You are the backup for request #{request.id} \"Contract\" (requested by Sam).")
    assert not to_owner.is_read and not to_backup.is_read
    assert to_owner.created_at == to_backup.created_at == request.timestamps[STAGES[0]]
    assert list_notifications(conn, "Sam") == []  # the requester is not told on submit


def test_only_the_owner_is_notified_when_there_is_no_backup(conn):
    create_request(conn, "Contract", "Sam", "Alex", now=T0)

    assert len(list_notifications(conn, "Alex")) == 1
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_a_rejected_request_leaves_no_notifications(conn):
    with pytest.raises(DocFlowError):
        create_request(conn, "Contract", "Sam", "Alex", backup="alex", now=T0)
    with pytest.raises(DocFlowError):
        create_request(conn, "", "Sam", "Alex", backup="Bo", now=T0)

    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
