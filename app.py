"""DocFlow Streamlit app: a request form, a status board and notifications.

Run with:  streamlit run app.py
"""

import logging
import os
from datetime import datetime

import streamlit as st

from docflow.core import (
    STAGE_LABELS,
    STAGES,
    STORAGE_ERRORS,
    DocFlowError,
    add_person,
    advance_request,
    assignable_people,
    can_confirm_delivery,
    choose_backup,
    choose_owner,
    confirm_delivery,
    connect,
    create_request,
    get_timeout_minutes,
    is_requester_only,
    list_notifications,
    list_requests,
    mark_notifications_read,
    next_stage,
    people,
    processing_readout,
    reassign_deadline,
    reassign_overdue,
    requests_by_stage,
    set_requester_only,
    set_timeout_minutes,
    turso_settings,
)

DB_PATH = os.environ.get("DOCFLOW_DB", "docflow.db")
# With Turso set up, the local file is only a replica; kept apart from DB_PATH so an
# existing local database is never opened as one.
REPLICA_PATH = os.environ.get("DOCFLOW_REPLICA", "docflow-replica.db")
CHECK_EVERY_SECONDS = 10
# Shown when storage fails; never includes the error text, which could name the database.
UNREACHABLE = "Can't reach the database right now. Nothing new was saved. Try again in a moment."
SAVE_FAILED = "Couldn't save that change. Check the board, then try again."
SUBMIT_FAILED = ("Couldn't save your request. What you typed is still in the form. "
                 "Check the Status board before resubmitting, in case it went through.")

log = logging.getLogger("docflow")

# Saved on first run so a fresh hosted database already has people to assign. They are
# ordinary saved people, not a limit: anyone saved later can be an owner or backup too.
SEED_PEOPLE = ["Ayma", "Kostas", "Alex", "Elina"]

ICON_PATH = os.path.join(os.path.dirname(__file__), "assets", "docflow-icon.png")

st.set_page_config(page_title="DocFlow", page_icon=ICON_PATH, layout="wide")


def read_secrets():
    """Streamlit secrets, or nothing if there is no secrets file."""
    try:
        return dict(st.secrets)
    except FileNotFoundError:
        return {}


@st.cache_resource
def get_conn():
    """The shared connection and a short description of where data is stored."""
    url, token = turso_settings(read_secrets(), os.environ)
    if url:
        conn, storage = connect(REPLICA_PATH, url, token), "Turso (kept across restarts)"
    else:
        conn, storage = connect(DB_PATH), f"local file {DB_PATH}"
    for name in SEED_PEOPLE:
        add_person(conn, name)
    return conn, storage


def storage_unavailable():
    """Tell the viewer storage is down, offer a retry and stop drawing the page."""
    log.warning("Storage unavailable", exc_info=True)
    st.error(UNREACHABLE)
    # Clearing the cache makes the next run connect and sync again from scratch.
    st.button("Try again", key="retry-storage", on_click=get_conn.clear)
    st.stop()


def save(action, *args):
    """Run a write for a button; on a storage failure say so and return False."""
    try:
        action(*args)
    except STORAGE_ERRORS:
        log.warning("Write failed", exc_info=True)
        st.error(SAVE_FAILED)
        return False
    return True


def format_time(value):
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.astimezone().strftime("%d %b %Y, %H:%M")


def format_duration(delta):
    seconds = round(delta.total_seconds())
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min {seconds:02d} s" if minutes else f"{seconds} s"


@st.fragment(run_every=CHECK_EVERY_SECONDS)
def reassignment_watch(conn):
    """Reassign overdue requests while anyone has the app open."""
    try:
        reassigned = reassign_overdue(conn)
    except STORAGE_ERRORS:
        log.warning("Overdue check failed", exc_info=True)
        st.warning("Can't reach the database to check for overdue requests. "
                   f"Checking again in {CHECK_EVERY_SECONDS} s.")
        return
    if reassigned:
        st.rerun(scope="app")


def sidebar(conn, storage):
    """Render the sidebar and return the name the viewer picked as themselves."""
    with st.sidebar:
        names = people(conn)
        viewer = st.selectbox(
            "You are", names, index=None, key="viewer",
            placeholder="Choose or type your name", accept_new_options=True,
            help="Used to show your notifications and let you confirm deliveries. "
                 "A new name you type is saved for next time.")
        if viewer and viewer.strip() and viewer.strip().casefold() not in {
                name.casefold() for name in names}:
            add_person(conn, viewer)
        if viewer and viewer.strip():
            only = st.checkbox(
                "I only submit requests", value=is_requester_only(conn, viewer),
                key=f"requester-only-{viewer.strip().casefold()}",
                help="Tick this to be left out of owner and backup assignment.")
            if only != is_requester_only(conn, viewer) and save(
                    set_requester_only, conn, viewer, only):
                st.rerun()
        st.header("Settings")
        minutes = st.number_input(
            "Reassign to backup after (minutes without action)",
            min_value=1, step=1, value=get_timeout_minutes(conn),
        )
        if minutes != get_timeout_minutes(conn) and save(set_timeout_minutes, conn, minutes):
            st.rerun()
        st.caption(f"Storage: {storage}")
    return viewer


def request_form(conn):
    # The form is cleared by moving to new widget keys after a successful submit, so a
    # failed submit keeps what was typed.
    round_ = st.session_state.setdefault("form_round", 0)
    with st.form(f"new_request-{round_}"):
        title = st.text_input("Document requested *", key=f"title-{round_}")
        requester = st.text_input("Requested by *", key=f"requester-{round_}")
        details = st.text_area("Details", key=f"details-{round_}")
        submitted = st.form_submit_button("Submit request")

    if submitted:
        try:
            pool = assignable_people(conn)
            if not pool:
                raise DocFlowError("Nobody is available to own this request yet. "
                                   "Add someone who handles requests in the sidebar.")
            owner = choose_owner(conn, pool)
            backup = choose_backup(conn, owner, pool)
            request = create_request(conn, title, requester, owner, details, backup)
        except STORAGE_ERRORS:
            log.warning("Submit failed", exc_info=True)
            st.error(SUBMIT_FAILED)
        except DocFlowError as err:
            st.error(str(err))
        else:
            st.session_state.form_round = round_ + 1
            st.session_state.form_notice = (
                f"Request #{request.id} received and assigned to {request.owner}.")
            st.rerun()
    notice = st.session_state.pop("form_notice", None)
    if notice:
        st.success(notice)


def status_board(conn, viewer):
    board = requests_by_stage(conn)
    timeout = get_timeout_minutes(conn)
    for column, stage in zip(st.columns(len(STAGES)), STAGES):
        with column:
            st.subheader(f"{STAGE_LABELS[stage]} ({len(board[stage])})")
            for request in board[stage]:
                request_card(conn, request, timeout, viewer)


def request_card(conn, request, timeout, viewer):
    with st.container(border=True):
        st.markdown(f"**#{request.id} {request.title}**")
        st.caption(f"Owner: {request.owner} · Requested by: {request.requester}")
        if request.reassigned_at:
            st.warning(f"Reassigned from {request.reassigned_from} "
                       f"at {format_time(request.reassigned_at)}")
        deadline = reassign_deadline(request, timeout)
        if deadline:
            st.caption(f"Goes to {request.backup} if no action by {format_time(deadline)}")
        if request.details:
            st.write(request.details)
        for stage in STAGES:
            reached = request.timestamps[stage]
            if reached:
                st.caption(f"{STAGE_LABELS[stage]}: {format_time(reached)}")
        if request.delivered_by:
            st.caption(f"Delivery confirmed by {request.delivered_by}")

        target = next_stage(request.stage)
        if target == STAGES[-1]:
            delivery_action(conn, request, viewer)
        elif target and st.button(f"Move to {STAGE_LABELS[target]}",
                                  key=f"advance-{request.id}"):
            if save(advance_request, conn, request.id):
                st.rerun()


def delivery_action(conn, request, viewer):
    if can_confirm_delivery(request, viewer):
        if st.button("Confirm delivered to client", key=f"deliver-{request.id}",
                     type="primary"):
            if save(confirm_delivery, conn, request.id, viewer):
                st.rerun()
    elif viewer:
        st.caption(f"Waiting for {request.owner} to confirm delivery to the client")
    else:
        st.caption(f"Waiting for {request.owner} to confirm delivery. "
                   "Pick your name under 'You are' to confirm.")


def notifications_panel(conn, viewer):
    names = people(conn)
    if not names:
        st.info("No notifications yet.")
        return
    lowered = [name.casefold() for name in names]
    default = lowered.index(viewer.casefold()) if viewer and viewer.casefold() in lowered else 0
    person = st.selectbox("Show notifications for", names, index=default)
    notes = list_notifications(conn, person)
    unread = sum(not n.is_read for n in notes)
    st.write(f"{unread} unread of {len(notes)}")
    if unread and st.button("Mark all as read") and save(mark_notifications_read, conn, person):
        st.rerun()
    for note in notes:
        with st.container(border=True):
            prefix = "" if note.is_read else "🔵 **New** · "
            st.markdown(f"{prefix}{note.message}")
            st.caption(format_time(note.created_at))


def readout_panel(conn):
    readout = processing_readout(list_requests(conn))
    if readout is None:
        st.info("No requests have been delivered yet.")
        return
    total, waiting, preparation = st.columns(3)
    total.metric("Median processing time", format_duration(readout.median_total))
    waiting.metric("Median waiting", format_duration(readout.median_waiting))
    preparation.metric("Median preparation", format_duration(readout.median_preparation))
    share = readout.waiting_share
    st.progress(share, text=f"Across all delivered requests, {share:.0%} of processing time "
                            f"was waiting and {1 - share:.0%} was preparation.")
    st.caption(
        f"Based on {readout.delivered} delivered request(s). Processing runs from received "
        "to delivered. Preparation is from 'In preparation' to 'Approved'; waiting is the "
        "rest, before preparation starts and after approval. Each median is taken "
        "separately, so the two parts need not add up to the total.")


def render(conn, storage):
    reassignment_watch(conn)
    viewer = sidebar(conn, storage)
    st.title("DocFlow")
    form_tab, board_tab, notes_tab, readout_tab = st.tabs(
        ["New request", "Status board", "Notifications", "Readout"])
    with form_tab:
        request_form(conn)
    with board_tab:
        status_board(conn, viewer)
    with notes_tab:
        notifications_panel(conn, viewer)
    with readout_tab:
        readout_panel(conn)


try:
    conn, storage = get_conn()
except STORAGE_ERRORS:
    storage_unavailable()
except DocFlowError as err:  # a settings mistake, such as only one Turso setting
    st.error(f"Cannot open the database: {err}")
    st.stop()
try:
    render(conn, storage)
except STORAGE_ERRORS:
    storage_unavailable()
