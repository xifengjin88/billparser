"""Disk storage behind one small interface, so S3 + Postgres can replace it later.

store/
  specs/<id>/v<N>.json      every version of every learned spec (never overwritten)
  specs/<id>/current.json   pointer: {"version": N}
  docs/<sha>.<ext>          raw uploads kept for regression tests (optional, see retain_docs)
  results/<sha>.json        result summary; results/<sha>.parquet canonical rows
  index.json                spec -> docs it parsed + fingerprints
  records.jsonl             learning records (one per parse)
  labels.jsonl              row labels + features (row classifier training data)
  review.jsonl              review queue
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "specs" / "builtin"


class Store:
    def __init__(self, root: str | Path = "store", retain_docs: bool = True):
        self.root = Path(root)
        self.retain_docs = retain_docs
        for d in ("specs", "docs", "results"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self.index = json.loads(self._index_path.read_text()) if self._index_path.exists() else {"specs": {}}
        self._builtin = {json.loads(p.read_text())["id"]: json.loads(p.read_text())
                         for p in sorted(BUILTIN_DIR.glob("*.json"))}

    # ---------------- specs
    def builtin_specs(self) -> list[dict]:
        return list(self._builtin.values())

    def learned_specs(self) -> list[dict]:
        out = []
        for d in sorted((self.root / "specs").iterdir()):
            cur = d / "current.json"
            if cur.exists():
                v = json.loads(cur.read_text())["version"]
                out.append(json.loads((d / f"v{v}.json").read_text()))
        return out

    def get_spec(self, spec_id: str, version: int | None = None) -> dict | None:
        if spec_id in self._builtin:
            return self._builtin[spec_id]
        d = self.root / "specs" / spec_id
        if not d.exists():
            return None
        v = version or json.loads((d / "current.json").read_text())["version"]
        p = d / f"v{v}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def save_spec(self, spec: dict, make_current: bool = True) -> dict:
        d = self.root / "specs" / spec["id"]
        d.mkdir(parents=True, exist_ok=True)
        versions = [int(p.stem[1:]) for p in d.glob("v*.json")]
        spec = dict(spec, version=max(versions, default=0) + 1, created_at=time.time())
        (d / f"v{spec['version']}.json").write_text(json.dumps(spec, indent=1))
        if make_current:
            (d / "current.json").write_text(json.dumps({"version": spec["version"]}))
        return spec

    def set_current(self, spec_id: str, version: int) -> None:
        (self.root / "specs" / spec_id / "current.json").write_text(json.dumps({"version": version}))

    def versions(self, spec_id: str) -> list[int]:
        d = self.root / "specs" / spec_id
        return sorted(int(p.stem[1:]) for p in d.glob("v*.json")) if d.exists() else []

    # ---------------- index: which docs each spec has parsed (regression set)
    def link(self, spec_id: str, sha: str, fingerprint: dict, accepted: dict) -> None:
        entry = self.index["specs"].setdefault(spec_id, {"docs": {}, "fingerprints": []})
        entry["docs"][sha] = accepted                          # {"rows": n, "total": x, "confidence": c}
        if fingerprint["hash"] not in [f["hash"] for f in entry["fingerprints"]]:
            entry["fingerprints"].append(fingerprint)
        self._save_index()

    def spec_docs(self, spec_id: str) -> dict[str, dict]:
        return self.index["specs"].get(spec_id, {}).get("docs", {})

    def fingerprints(self) -> list[tuple[str, dict]]:
        return [(sid, fp) for sid, e in self.index["specs"].items() for fp in e.get("fingerprints", [])]

    def _save_index(self) -> None:
        self._index_path.write_text(json.dumps(self.index, indent=1))

    # ---------------- docs and results
    def save_doc(self, sha: str, data: bytes, name: str) -> None:
        if self.retain_docs:
            ext = Path(name).suffix or ".bin"
            p = self.root / "docs" / f"{sha}{ext}"
            if not p.exists():
                p.write_bytes(data)
                (self.root / "docs" / f"{sha}.name").write_text(name)

    def load_doc(self, sha: str) -> tuple[bytes, str] | None:
        hits = [p for p in (self.root / "docs").glob(f"{sha}.*") if p.suffix != ".name"]
        if not hits:
            return None
        name_p = self.root / "docs" / f"{sha}.name"
        return hits[0].read_bytes(), (name_p.read_text() if name_p.exists() else hits[0].name)

    def save_result(self, sha: str, summary: dict, rows: pd.DataFrame) -> None:
        (self.root / "results" / f"{sha}.json").write_text(json.dumps(summary, indent=1, default=str))
        if rows is not None and not rows.empty:
            rows.astype({c: "string" for c in rows.columns if rows[c].dtype == object}).to_parquet(
                self.root / "results" / f"{sha}.parquet", index=False)

    def get_result(self, sha: str) -> dict | None:
        p = self.root / "results" / f"{sha}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def get_rows(self, sha: str) -> pd.DataFrame | None:
        p = self.root / "results" / f"{sha}.parquet"
        return pd.read_parquet(p) if p.exists() else None

    # ---------------- learning + review
    def append(self, name: str, record: dict) -> None:
        with open(self.root / f"{name}.jsonl", "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def read(self, name: str) -> list[dict]:
        p = self.root / f"{name}.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []

    def rewrite(self, name: str, records: list[dict]) -> None:
        p = self.root / f"{name}.jsonl"
        p.write_text("".join(json.dumps(r, default=str) + "\n" for r in records))
