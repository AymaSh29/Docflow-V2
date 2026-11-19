"""The Streamlit app when storage fails: a clear message, a retry, and no lost typing.

Each test runs in its own temporary folder with non-empty fake secrets, so AppTest
never falls back to a real .streamlit/secrets.toml or a real Turso database.
"""

import sqlite3
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")
LOCAL = {"DOCFLOW_TEST": "1"}  # no Turso settings, so the app uses the local file
UNREACHABLE_TURSO = {"TURSO_DATABASE_URL": "http://127.0.0.1:9", "TURSO_AUTH_TOKEN": "fake"}
UNREACHABLE = "Can't reach the database right now. Nothing new was saved. Try again in a moment."
SUBMIT_FAILED = ("Couldn't save your request. What you typed is still in the form. "
                 "Check the Status board before resubmitting, in case it went through.")


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOCFLOW_DB", str(tmp_path / "docflow.db"))
    monkeypatch.setenv("DOCFLOW_REPLICA", str(tmp_path / "replica.db"))
    st.cache_resource.clear()  # the cached connection outlives a single AppTest
    yield tmp_path
    st.cache_resource.clear()


def start(secrets):
    app = AppTest.from_file(APP, default_timeout=30)
    app.secrets = dict(secrets)
    return app.run()


def submit_button(app):
    return next(b for b in app.button if b.label == "Submit request")


def test_unreachable_storage_shows_message_and_retry(folder):
    app = start(UNREACHABLE_TURSO)

    assert not app.exception
    assert [e.value for e in app.error] == [UNREACHABLE]
    assert app.button(key="retry-storage").label == "Try again"
    assert not app.title  # the rest of the page is not drawn


def test_try_again_reconnects_once_storage_is_back(folder):
    app = start(UNREACHABLE_TURSO)

    app.secrets = dict(LOCAL)  # storage is reachable again
    app.button(key="retry-storage").click().run()

    assert not app.exception and not app.error
    assert app.title[0].value == "DocFlow"
    assert any("Storage: local file" in c.value for c in app.sidebar.caption)


def test_failed_submit_keeps_what_was_typed(folder):
    app = start(LOCAL)
    assert any("Storage: local file" in c.value for c in app.sidebar.caption)
    app.text_input(key="title-0").input("Contract")
    app.text_input(key="requester-0").input("Sam")
    app.text_area(key="details-0").input("urgent")

    blocker = sqlite3.connect(folder / "docflow.db")
    blocker.execute("BEGIN IMMEDIATE")  # holds the write lock; reads still work
    try:
        submit_button(app).click().run()
        assert not app.exception
        assert [e.value for e in app.error] == [SUBMIT_FAILED]
        assert app.text_input(key="title-0").value == "Contract"
        assert app.text_input(key="requester-0").value == "Sam"
        assert app.text_area(key="details-0").value == "urgent"
    finally:
        blocker.rollback()
        blocker.close()

    submit_button(app).click().run()  # storage is back: the same text goes through
    assert not app.exception and not app.error
    assert app.success[0].value.startswith("Request #1 received")
    assert app.text_input(key="title-1").value == ""  # the form is cleared after success
