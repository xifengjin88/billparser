"""The service catalog from the price catalog's own lists (service_catalog.py), the fetch scripts' JSON for the rest."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import billparse.analyze as analyze_mod  # noqa: E402
from billparse import service_catalog  # noqa: E402
from billparse.db import engine_for  # noqa: E402
from prices.load import load_snapshot  # noqa: E402
from svcres.catalog import Catalog, build  # noqa: E402
from svcres.normalize import code_key  # noqa: E402
from svcres.resolver import Resolver  # noqa: E402

FX = ROOT / "tests" / "fixtures"
AWS = FX / "prices" / "download"
AZURE = FX / "prices" / "azure" / "download"
GCP = FX / "prices" / "gcp" / "download"


@pytest.fixture
def engine(tmp_path):
    e = engine_for(f"sqlite:///{tmp_path / 'p.db'}")
    for d in (AWS, AZURE, GCP):
        load_snapshot(e, d)
    return e


def test_the_price_catalog_gives_each_providers_list(engine):
    rows = service_catalog.catalog_rows(engine)
    azure = {r["productId"]: r for r in rows["azure"]}
    assert azure and all(r["serviceName"] and r["serviceFamily"] for r in azure.values())
    aws = {r["code"]: r["name"] for r in rows["aws"]}
    assert aws["AmazonEC2"] == "Amazon Elastic Compute Cloud"
    assert {r["code"]: r["name"] for r in rows["gcp"]}["6F81-5844-456A"] == "Compute Engine"


def test_the_database_adds_to_the_json_and_known_names_stay(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "azure_products.json").write_text(json.dumps({"OLD1": {
        "productId": "OLD1", "productName": "Old Product", "serviceName": "Old Service", "serviceFamily": "Compute"}}))
    (raw / "aws_services.json").write_text(json.dumps({"AmazonEC2": {"names": ["EC2 (old name)"]}}))
    db = {"azure": [{"productId": "NEW1", "productName": "Azure Brand New Product", "serviceName": "Brand New Service",
                     "serviceId": "SVC1", "serviceFamily": "Analytics"}],
          "aws": [{"code": "AmazonEC2", "name": "Amazon Elastic Compute Cloud"}],
          "gcp": [{"code": "AAAA-BBBB-CCCC", "name": "Brand New GCP Service"}]}
    cat = build(raw, db)
    r = Resolver(cat, tmp_path)
    assert r.resolve("Azure Brand New Product", "azure").service == "Brand New Service"     # only in the database
    assert r.resolve("Old Product", "azure").service == "Old Service"                      # only in the JSON
    ec2 = cat.codes[f"aws:{code_key('AmazonEC2')}"]
    assert cat.services[ec2]["name"] == "EC2 (old name)"           # a known service keeps its name: its key
    assert r.resolve("Amazon Elastic Compute Cloud", "aws").service == "EC2 (old name)"   # the database's name too
    db["aws"].append({"code": "AmazonNewThing", "name": "Amazon New Thing"})
    assert r.resolve("Amazon New Thing", "aws").service is None
    assert Resolver(build(raw, db), tmp_path).resolve("Amazon New Thing", "aws").service == "Amazon New Thing"
    assert r.resolve("AAAA-BBBB-CCCC", "gcp").service == "Brand New GCP Service"
    assert cat.stats()["sources"] == {"azure": {"db": 1, "raw": 1}, "aws": {"db": 1, "raw": 1},
                                      "gcp": {"db": 1, "raw": 0}}


def test_rebuild_writes_the_catalog_and_resolvers_load_it_again(engine, tmp_path, monkeypatch):
    data = tmp_path / "svc"
    shutil.copytree(FX / "raw", data / "raw")
    monkeypatch.setattr(analyze_mod, "_RESOLVER", None)
    first = analyze_mod.get_resolver(data)
    stats = service_catalog.rebuild(engine, data)
    assert stats["sources"]["azure"]["db"] > 0 and stats["sources"]["aws"]["db"] > 0
    os.utime(data / "catalog.msgpack", (1, 2_000_000_000))       # a newer file than the one loaded
    second = analyze_mod.get_resolver(data)
    assert second is not first and Catalog.load(data / "catalog.msgpack").sources == stats["sources"]
    assert analyze_mod.get_resolver(data) is second                        # unchanged file: the same resolver
    monkeypatch.setattr(analyze_mod, "_RESOLVER", None)


def test_resolution_finds_a_service_only_the_price_catalog_has(engine, tmp_path, monkeypatch):
    data = tmp_path / "svc"
    (data / "raw").mkdir(parents=True)                                      # no JSON at all
    service_catalog.rebuild(engine, data)
    monkeypatch.setattr(analyze_mod, "_RESOLVER", None)
    rows = pd.DataFrame({"service_raw": ["Virtual Machines", "Compute Engine"], "provider": ["azure", "gcp"],
                         "billed_cost": [1.0, 1.0]})
    analyze_mod.resolve_services(rows, data)
    monkeypatch.setattr(analyze_mod, "_RESOLVER", None)
    assert rows["service_code"].notna().all()


# ---------------------------------------------------------------- the code lists (service_product) and seed aliases
def test_a_code_list_in_the_table_resolves_and_a_new_download_replaces_it(tmp_path):
    e = engine_for(f"sqlite:///{tmp_path / 'p.db'}")
    rows = [{"product_id": "P1", "product_name": "Azure Shiny Product", "service_id": "S1",
             "service_name": "Shiny Service", "family": "Compute"}]
    assert service_catalog.load_products(e, "azure", rows) == 1
    data = tmp_path / "svc"
    (data / "raw").mkdir(parents=True)
    service_catalog.rebuild(e, data)
    cat = Catalog.load(data / "catalog.msgpack")
    assert Resolver(cat, data).resolve("Azure Shiny Product", "azure").service == "Shiny Service"
    assert cat.sources["azure"]["db"] == 1
    # the next download replaces the list; an empty one changes nothing
    service_catalog.load_products(e, "azure", [dict(rows[0], product_id="P2", product_name="Azure Other")])
    assert service_catalog.load_products(e, "azure", []) == 0
    got = service_catalog.catalog_rows(e)["azure"]
    assert [r["productId"] for r in got] == ["P2"]


def test_the_json_lists_load_into_the_table(tmp_path):
    rows = service_catalog.products_from_json(FX / "raw")
    assert rows["azure"] and rows["aws"] and all(r["product_id"] for p in rows.values() for r in p)
    e = engine_for(f"sqlite:///{tmp_path / 'p.db'}")
    for p, r in rows.items():
        assert service_catalog.load_products(e, p, r) == len({(x["product_id"], x["product_name"]) for x in r})


def test_the_built_in_aliases_are_seeded_once_and_never_overwrite(tmp_path):
    e = engine_for(f"sqlite:///{tmp_path / 'p.db'}")
    from billparse.synonyms import ServiceSynonymStore
    store = ServiceSynonymStore(e)
    cat = build(FX / "raw")
    rows = service_catalog.seed_rows(cat)
    assert rows and {r["scope"] for r in rows} >= {"aws"}
    mine = rows[0]
    store.add(mine["scope"], mine["label"], "aws:Something Else", source="admin", by="me")   # a person's first
    added = service_catalog.seed_synonyms(e, cat)
    assert added == len(rows) - 1
    by = {(r["scope"], r["label"]): r for r in store.entries()}
    assert by[(mine["scope"], mine["label"])]["service_key"] == "aws:Something Else"
    assert {r["source"] for k, r in by.items() if k != (mine["scope"], mine["label"])} == {"seed"}
    store.remove(rows[1]["scope"], rows[1]["label"])                # removed by a person: not seeded again
    assert service_catalog.seed_synonyms(e, cat) == 0
    assert (rows[1]["scope"], rows[1]["label"]) not in {(r["scope"], r["label"]) for r in store.entries()}
