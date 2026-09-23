# DocFlow

A shared tool for tracking document requests. Each request is submitted through a form,
assigned to an owner, and moved through five stages on a status board:
received → picked up → in preparation → approved → delivered. The time each stage was
reached is recorded.

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
