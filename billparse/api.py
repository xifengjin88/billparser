"""HTTP API.

    uvicorn billparse.api:app --reload
    curl -F file=@invoice.pdf localhost:8000/parse
"""

from __future__ import annotations

from fastapi import FastAPI, File, HTTPException, Query, UploadFile

from .learn import metrics, run_nightly
from .pipeline import Pipeline, Settings

app = FastAPI(title="billparse", version="0.1.0")
pipe = Pipeline(Settings())

MAX_BYTES = 500 * 1024 * 1024


@app.post("/parse")
async def parse(file: UploadFile = File(...), force: bool = Query(False)) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large")
    return pipe.parse_bytes(data, file.filename or "upload", force=force)


@app.get("/results/{sha}")
def result(sha: str) -> dict:
    r = pipe.store.get_result(sha)
    if not r:
        raise HTTPException(404, "Not found")
    return r


@app.get("/results/{sha}/rows")
def rows(sha: str, limit: int = 1000) -> list[dict]:
    df = pipe.store.get_rows(sha)
    if df is None:
        raise HTTPException(404, "Not found")
    return df.head(limit).astype(object).where(df.head(limit).notna(), None).to_dict("records")


@app.get("/review")
def review() -> list[dict]:
    return [r for r in pipe.store.read("review") if r.get("open")]


@app.get("/specs")
def specs() -> list[dict]:
    out = [{"id": s["id"], "trust": "builtin", "format": s["meta"].get("format")} for s in pipe.store.builtin_specs()]
    out += [{"id": s["id"], "trust": s.get("trust"), "version": s.get("version"),
             "docs": len(pipe.store.spec_docs(s["id"]))} for s in pipe.store.learned_specs()]
    return out


@app.post("/specs/{spec_id}/approve")
def approve(spec_id: str) -> dict:
    s = pipe.store.get_spec(spec_id)
    if not s or s.get("trust") == "builtin":
        raise HTTPException(404, "No learned spec with that id")
    return {"version": pipe.store.save_spec(dict(s, trust="verified"))["version"]}


@app.post("/nightly")
def nightly() -> dict:
    return run_nightly(pipe)


@app.get("/metrics")
def get_metrics() -> dict:
    return metrics(pipe.store.read("records"))
