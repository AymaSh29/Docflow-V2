"""DocFlow Streamlit app: a request form and a status board.

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
    next_stage,
    requests_by_stage,
)

DB_PATH = os.environ.get("DOCFLOW_DB", "docflow.db")

st.set_page_config(page_title="DocFlow", layout="wide")


@st.cache_resource
def get_conn():
    return connect(DB_PATH)


def format_time(iso):
    return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y, %H:%M")


def request_form(conn):
    with st.form("new_request", clear_on_submit=True):
        title = st.text_input("Document requested *")
        requester = st.text_input("Requested by *")
        owner = st.text_input("Owner *")
        details = st.text_area("Details")
        submitted = st.form_submit_button("Submit request")

    if submitted:
        try:
            request = create_request(conn, title, requester, owner, details)
        except DocFlowError as err:
            st.error(str(err))
        else:
            st.success(f"Request #{request.id} received and assigned to {request.owner}.")


def status_board(conn):
    board = requests_by_stage(conn)
    for column, stage in zip(st.columns(len(STAGES)), STAGES):
        with column:
            st.subheader(f"{STAGE_LABELS[stage]} ({len(board[stage])})")
            for request in board[stage]:
                request_card(conn, request)


def request_card(conn, request):
    with st.container(border=True):
        st.markdown(f"**#{request.id} {request.title}**")
        st.caption(f"Owner: {request.owner} · Requested by: {request.requester}")
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


conn = get_conn()
st.title("DocFlow")
form_tab, board_tab = st.tabs(["New request", "Status board"])
with form_tab:
    request_form(conn)
with board_tab:
    status_board(conn)
