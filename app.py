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
    advance_request,
    connect,
    create_request,
    get_timeout_minutes,
    list_notifications,
    mark_notifications_read,
    next_stage,
    people,
    reassign_deadline,
    reassign_overdue,
    requests_by_stage,
    set_timeout_minutes,
)

DB_PATH = os.environ.get("DOCFLOW_DB", "docflow.db")
CHECK_EVERY_SECONDS = 10

st.set_page_config(page_title="DocFlow", layout="wide")


@st.cache_resource
def get_conn():
    return connect(DB_PATH)


def format_time(value):
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.astimezone().strftime("%d %b %Y, %H:%M")


@st.fragment(run_every=CHECK_EVERY_SECONDS)
def reassignment_watch(conn):
    """Reassign overdue requests while anyone has the app open."""
    if reassign_overdue(conn):
        st.rerun(scope="app")


def settings_sidebar(conn):
    with st.sidebar:
        st.header("Settings")
        minutes = st.number_input(
            "Reassign to backup after (minutes without action)",
            min_value=1, step=1, value=get_timeout_minutes(conn),
        )
        if minutes != get_timeout_minutes(conn):
            set_timeout_minutes(conn, minutes)
            st.rerun()


def request_form(conn):
    with st.form("new_request", clear_on_submit=True):
        title = st.text_input("Document requested *")
        requester = st.text_input("Requested by *")
        owner = st.text_input("Owner *")
        backup = st.text_input(
            "Backup", help="Takes over if the owner does not act in time. "
                           "Leave blank to never reassign.")
        details = st.text_area("Details")
        submitted = st.form_submit_button("Submit request")

    if submitted:
        try:
            request = create_request(conn, title, requester, owner, details, backup)
        except DocFlowError as err:
            st.error(str(err))
        else:
            st.success(f"Request #{request.id} received and assigned to {request.owner}.")


def status_board(conn):
    board = requests_by_stage(conn)
    timeout = get_timeout_minutes(conn)
    for column, stage in zip(st.columns(len(STAGES)), STAGES):
        with column:
            st.subheader(f"{STAGE_LABELS[stage]} ({len(board[stage])})")
            for request in board[stage]:
                request_card(conn, request, timeout)


def request_card(conn, request, timeout):
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

        target = next_stage(request.stage)
        if target and st.button(f"Move to {STAGE_LABELS[target]}", key=f"advance-{request.id}"):
            advance_request(conn, request.id)
            st.rerun()


def notifications_panel(conn):
    names = people(conn)
    if not names:
        st.info("No notifications yet.")
        return
    person = st.selectbox("Show notifications for", names)
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


conn = get_conn()
reassignment_watch(conn)
settings_sidebar(conn)
st.title("DocFlow")
form_tab, board_tab, notes_tab = st.tabs(["New request", "Status board", "Notifications"])
with form_tab:
    request_form(conn)
with board_tab:
    status_board(conn)
with notes_tab:
    notifications_panel(conn)
