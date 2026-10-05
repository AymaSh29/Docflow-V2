"""DocFlow Streamlit app: a request form, a status board and notifications.

Run with:  streamlit run app.py
"""

import os
from datetime import datetime

import streamlit as st

from docflow.core import (
    STAGE_LABELS,
    STAGES,
    DocFlowError,
    add_person,
    advance_request,
    can_confirm_delivery,
    choose_backup,
    choose_owner,
    confirm_delivery,
    connect,
    create_request,
    get_timeout_minutes,
    list_notifications,
    list_requests,
    mark_notifications_read,
    next_stage,
    people,
    processing_readout,
    reassign_deadline,
    reassign_overdue,
    requests_by_stage,
    set_timeout_minutes,
    turso_settings,
)

DB_PATH = os.environ.get("DOCFLOW_DB", "docflow.db")
# With Turso set up, the local file is only a replica; kept apart from DB_PATH so an
# existing local database is never opened as one.
REPLICA_PATH = "docflow-replica.db"
CHECK_EVERY_SECONDS = 10
# Offered under "You are" from the start, since a hosted app begins with an empty database.
TEAM = ["Ayma", "Kostas", "Alex", "Elina"]

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
    for name in TEAM:
        add_person(conn, name)
    return conn, storage


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
    if reassign_overdue(conn):
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
        st.header("Settings")
        minutes = st.number_input(
            "Reassign to backup after (minutes without action)",
            min_value=1, step=1, value=get_timeout_minutes(conn),
        )
        if minutes != get_timeout_minutes(conn):
            set_timeout_minutes(conn, minutes)
            st.rerun()
        st.caption(f"Storage: {storage}")
    return viewer


def request_form(conn):
    with st.form("new_request", clear_on_submit=True):
        title = st.text_input("Document requested *")
        requester = st.text_input("Requested by *")
        details = st.text_area("Details")
        submitted = st.form_submit_button("Submit request")

    if submitted:
        try:
            owner = choose_owner(conn, TEAM)
            backup = choose_backup(conn, owner, TEAM)
            request = create_request(conn, title, requester, owner, details, backup)
        except DocFlowError as err:
            st.error(str(err))
        else:
            st.success(f"Request #{request.id} received and assigned to {request.owner}.")


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
            advance_request(conn, request.id)
            st.rerun()


def delivery_action(conn, request, viewer):
    if can_confirm_delivery(request, viewer):
        if st.button("Confirm delivered to client", key=f"deliver-{request.id}",
                     type="primary"):
            confirm_delivery(conn, request.id, viewer)
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
    if unread and st.button("Mark all as read"):
        mark_notifications_read(conn, person)
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


try:
    conn, storage = get_conn()
except DocFlowError as err:
    st.error(f"Cannot open the database: {err}")
    st.stop()
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
