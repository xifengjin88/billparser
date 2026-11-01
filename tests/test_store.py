"""The store's writes that several workers can make at once."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlmodel import Session  # noqa: E402

from billparse.store import Store  # noqa: E402

FP = {"hash": "9cb093f49a1a69ea", "file_type": "tf", "headers": []}


def test_link_twice_keeps_one_fingerprint_and_updates_the_doc(tmp_path):
    store = Store(tmp_path / "store")
    store.link("iac", "a" * 64, FP, {"rows": 3, "total": 10.0, "confidence": 0.9})
    store.link("iac", "a" * 64, FP, {"rows": 4, "total": 12.0, "confidence": 0.95})
    assert store.spec_docs("iac")["a" * 64]["rows"] == 4
    assert [s for s, _ in store.fingerprints()] == ["iac"]


def test_a_fingerprint_another_worker_just_wrote_is_no_conflict(tmp_path, monkeypatch):
    """Two workers parse two files of one layout at once: the other one writes the layout's fingerprint after this
    one looked for it (a lookup sees no row) and before this one inserts it."""
    root = tmp_path / "store"
    Store(root).link("iac", "b" * 64, FP, {"rows": 1, "total": 1.0, "confidence": 0.9})     # the other worker
    monkeypatch.setattr(Session, "get", lambda *a, **k: None)                            # this one looked before
    Store(root).link("iac", "a" * 64, FP, {"rows": 1, "total": 1.0, "confidence": 0.9})
    assert set(Store(root).spec_docs("iac")) == {"a" * 64, "b" * 64}
