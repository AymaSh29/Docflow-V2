# DocFlow

A shared tool for tracking document requests. Each request is submitted through a form,
assigned to an owner, and moved through five stages on a status board:
received → picked up → in preparation → approved → delivered. The time each stage was
reached is recorded.

Delivery to the client is confirmed by the preparer (the request's current owner) with one
click. Pick your name under "You are" in the sidebar; on approved requests you own, a
"Confirm delivered to client" button appears, and the card records who confirmed. A new
name typed into "You are" is saved and offered to everyone from then on. There
are no logins, so the name picker reflects the workflow rather than enforcing security.

Each request can name a backup. If the owner does not move the request to its next stage
within the timeout (set in the sidebar, in minutes; 2 by default for demos), it is
reassigned to the backup, and both people get a message in the Notifications tab.
A request is reassigned at most once and never after it is delivered. The check runs
every 10 seconds while at least one person has the app open.

## Run

```
pip install -r requirements.txt
streamlit run app.py
```

Data is stored in `docflow.db` (set `DOCFLOW_DB` to use a different file).

## Test

```
pytest
```
