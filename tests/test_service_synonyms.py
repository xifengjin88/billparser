"""Service-name synonyms in the database (billparse/synonyms.py): concurrent writes, other processes' changes, the
one-time import of the old synonyms.json, removal."""

import json
import threading
from pathlib import Path

import pytest

from billparse import synonyms
from billparse.db import engine_for
from billparse.synonyms import IMPORT_STREAM, ServiceSynonymStore
from svcres.catalog import Catalog, build
from svcres.resolver import Resolver

RAW = Path(__file__).parent / "fixtures" / "raw"


@pytest.fixture
def catalog(tmp_path):
    build(RAW).save(tmp_path / "catalog.msgpack")
    return Catalog.load(tmp_path / "catalog.msgpack")


@pytest.fixture
def engine(tmp_path):
    return engine_for(f"sqlite:///{tmp_path / 'billparse.db'}")


def test_concurrent_adds_lose_nothing(engine):
    """Two writers (two processes' stores) adding at the same time: every synonym is kept."""
    a, b = ServiceSynonymStore(engine), ServiceSynonymStore(engine)

    def add(store, prefix):
        for i in range(40):
            store.add("*", f"{prefix} label {i}", "aws:Amazon Elastic Compute Cloud", "review")

    threads = [threading.Thread(target=add, args=(s, p)) for s, p in ((a, "a"), (b, "b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = ServiceSynonymStore(engine).current()
    assert len(got) == 80
    assert {f"*|{p} label {i}" for p in "ab" for i in range(40)} == set(got)


def test_another_process_sees_a_new_synonym_after_a_reload(engine, catalog, tmp_path, monkeypatch):
    one = Resolver(catalog, tmp_path, synonyms=ServiceSynonymStore(engine))
    other_store = ServiceSynonymStore(engine)
    other = Resolver(catalog, tmp_path, synonyms=other_store)
    assert other.resolve("Zorblax Hosting", "aws").service_key is None
    one.add_synonym("Zorblax Hosting", "aws:Amazon Elastic Compute Cloud", provider="aws", source="admin", by="a@b.c")
    assert one.resolve("Zorblax Hosting", "aws").method == "synonym"          # the writer: at once
    assert other.resolve("Zorblax Hosting", "aws").service_key is None              # the other: not before its next check
    monkeypatch.setattr(synonyms, "REFRESH_SECONDS", 0.0)
    assert other.resolve("Zorblax Hosting", "aws").method == "synonym"
    one.remove_synonym("aws", "zorblax hosting")
    assert other.resolve("Zorblax Hosting", "aws").service_key is None
    assert ServiceSynonymStore(engine).entries() == []


def test_a_reload_drops_the_cached_picks_a_new_synonym_covers(engine, catalog, tmp_path, monkeypatch):
    """Another process's synonym beats a fuzzy pick this one cached, for the label and labels it starts."""
    monkeypatch.setattr(synonyms, "REFRESH_SECONDS", 0.0)
    res = Resolver(catalog, tmp_path, synonyms=ServiceSynonymStore(engine))
    assert res.resolve("Big Query").method == "fuzzy"
    assert res.resolve("Big Query").method.startswith("cache:")
    ServiceSynonymStore(engine).add("*", "big", "azure:Storage", "admin")
    r = res.resolve("Big Query")
    assert (r.service_key, r.method) == ("azure:Storage", "synonym-prefix")


def test_a_cached_pick_from_the_file_gives_way_to_a_synonym(engine, catalog, tmp_path):
    """A process that starts with a cache file another process wrote before it saw a synonym."""
    store = ServiceSynonymStore(engine)
    store.add("*", "big query", "azure:Storage", "admin")
    res = Resolver(catalog, tmp_path, synonyms=store)
    res.cache["*|big query"] = {"service_key": "gcp:BigQuery", "method": "fuzzy", "score": 95.0}
    assert res.resolve("Big Query").method == "synonym"
    assert "*|big query" not in res.cache


def test_the_old_file_is_imported_once(engine, tmp_path):
    old = {"azure|sql db": "azure:SQL Database", "*|cloud hosting": "aws:Amazon Elastic Compute Cloud", "bad": "x"}
    path = tmp_path / "synonyms.json"
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    first, second = ServiceSynonymStore(engine, path), ServiceSynonymStore(engine, path)
    assert first.current() == {k: v for k, v in old.items() if "|" in k}
    assert second.current() == first.current()                             # no second import, no error
    assert {e["source"] for e in first.entries()} == {"review"}
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM journal WHERE stream = ?", (IMPORT_STREAM,)).scalar() == 1
    assert path.read_bytes() == before                                     # the file stays as it was
    # emptied later: the file is not imported again
    for k in old:
        if "|" in k:
            first.remove(*k.split("|"))
    assert ServiceSynonymStore(engine, path).current() == {}


def test_two_processes_importing_at_once(engine, tmp_path):
    path = tmp_path / "synonyms.json"
    path.write_text(json.dumps({f"*|label {i}": "azure:Storage" for i in range(50)}))
    stores = [ServiceSynonymStore(engine, path) for _ in range(4)]
    threads = [threading.Thread(target=s.current) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(len(s.current()) == 50 for s in stores)


def test_remove_and_the_record_of_a_change(engine):
    store = ServiceSynonymStore(engine)
    store.add("aws", "big vms", "aws:Amazon Elastic Compute Cloud", "review", "x@y.z")
    store.add("aws", "big vms", "aws:Amazon Simple Storage Service", "admin", "a@b.c")
    [e] = store.entries()
    assert (e["service_key"], e["source"], e["updated_by"]) == ("aws:Amazon Simple Storage Service", "admin", "a@b.c")
    assert e["created"] <= e["updated"]
    assert store.current() == {"aws|big vms": "aws:Amazon Simple Storage Service"}
    assert store.remove("aws", "big vms") and not store.remove("aws", "big vms")
    assert store.current() == {}
    with pytest.raises(ValueError):
        store.add("*", "x", "y", "spec")


def test_get_resolver_uses_the_database(tmp_path, monkeypatch):
    from billparse import analyze
    cat_dir = tmp_path / "data"
    cat_dir.mkdir()
    build(RAW).save(cat_dir / "catalog.msgpack")
    (cat_dir / "synonyms.json").write_text(json.dumps({"azure|sql db": "azure:SQL Database"}))
    monkeypatch.setattr(analyze, "_RESOLVER", None)
    res = analyze.get_resolver(cat_dir)
    assert isinstance(res.store, ServiceSynonymStore)
    assert res.resolve("SQL DB", "azure").service == "SQL Database"        # imported from the old file
    analyze._RESOLVER = None
