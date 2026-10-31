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


# ---------------------------------------------------------------- the fetch scripts write the table
def _script(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"{name}_test", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fetch_aws_writes_codes_and_names_to_the_table(tmp_path, monkeypatch):
    """Codes from the offer index; names from the Query API, else the loaded price files, else the built-in ones."""
    url = f"sqlite:///{tmp_path / 'p.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    load_snapshot(engine_for(url), AWS)                      # its price files name AmazonEC2
    mod = _script("fetch_aws")
    monkeypatch.setattr(mod, "fetch_codes", lambda: ["AmazonEC2", "AmazonS3", "AWSNewThing"])
    monkeypatch.setattr(mod, "fetch_names", lambda codes: {"AmazonS3": ["Amazon Simple Storage Service"]})
    out = tmp_path / "aws.json"
    assert mod.main(["--json", str(out)]) == 0
    rows = {r["code"]: r["name"] for r in service_catalog.catalog_rows(engine_for(url))["aws"] if "n" not in r}
    assert rows["AmazonS3"] == "Amazon Simple Storage Service"            # the Query API
    assert rows["AmazonEC2"] == "Amazon Elastic Compute Cloud"            # the price files
    assert "AWSNewThing" in rows                                          # a code with no name is still listed
    assert json.loads(out.read_text())["AmazonS3"] == {"names": ["Amazon Simple Storage Service"]}
    assert (Path(os.environ["CATALOG_DIR"]) / "catalog.msgpack").is_file()


def test_fetch_azure_writes_the_whole_list_and_never_a_part(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'p.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    mod = _script("fetch_azure")
    got = {"P1": {"productId": "P1", "productName": "Azure Shiny", "serviceName": "Shiny", "serviceId": "S1",
                  "serviceFamily": "Compute"}}
    monkeypatch.setattr(mod, "fetch", lambda out, flt, pages: got)
    assert mod.main(["--out", str(tmp_path / "az.json")]) == 0
    assert [r["productId"] for r in service_catalog.catalog_rows(engine_for(url))["azure"]] == ["P1"]
    monkeypatch.setattr(mod, "fetch", lambda out, flt, pages: {})
    assert mod.main(["--out", str(tmp_path / "az.json"), "--max-pages", "1"]) == 0   # a part: the table stays
    assert [r["productId"] for r in service_catalog.catalog_rows(engine_for(url))["azure"]] == ["P1"]


def test_an_azure_meter_name_resolves_to_its_service(engine, tmp_path):
    """Reseller and invoice files print the meter's name ("D4s v5", "P10 LRS Disk"); the price catalog knows each
    meter's service. A name that two services share is left out (it would be a guess)."""
    rows = service_catalog.catalog_rows(engine)
    meters = {r["name"]: r["serviceName"] for r in rows["azure_meters"]}
    assert meters["D4s v5"] == "Virtual Machines"
    cat = build(tmp_path, rows)
    r = Resolver(cat, tmp_path).resolve("D4s v5", "azure")
    assert r.service == "Virtual Machines" and r.method == "name"
    assert cat.sources["azure"]["meters"] == len(meters)


def test_a_name_several_azure_services_share_is_never_guessed(tmp_path):
    db = {"azure": [{"productId": f"P{i}", "productName": f"{s} product", "serviceName": s, "serviceId": f"S{i}",
                     "serviceFamily": "Databases"} for i, s in enumerate(["SQL Database", "Azure Database for MySQL"])],
          "azure_meters": [{"name": "General Purpose Data Stored", "serviceName": "SQL Database"}],
          "azure_meters_ambiguous": [{"name": "vCore", "services": ["Azure Database for MySQL", "SQL Database"]}]}
    r = Resolver(build(tmp_path, db), tmp_path)
    got = r.resolve("vCore", "azure")
    assert got.service is None and got.method == "ambiguous" and len(got.candidates) == 2
    fam = r.resolve("Databases", "azure")                     # a service family, not one service
    assert fam.service is None and fam.method == "ambiguous"
    assert r.resolve("General Purpose Data Stored", "azure").service == "SQL Database"


def test_a_finished_azure_fetch_deletes_its_save_files(tmp_path, monkeypatch):
    """A stopped run continues from its save files; a finished one deletes them, so the next run fetches again
    (a finished listing's save file has no next page: read again, it would return the old products)."""
    mod = _script("fetch_azure")
    pages = {"n": 0}

    def get(session, url, params, tries=6):
        pages["n"] += 1
        return {"Items": [{"productId": "P1", "productName": "Azure Shiny", "serviceName": "Shiny",
                           "serviceId": "S1", "serviceFamily": "Compute"}], "NextPageLink": None}
    monkeypatch.setattr(mod, "_get", get)
    monkeypatch.setattr(mod, "FAMILIES", ["Compute"])
    out = tmp_path / "azure_products.json"
    assert set(mod.fetch(out, None, None)) == {"P1"} and pages["n"] == 1
    assert not out.with_suffix(".parts").exists()
    mod.fetch(out, None, None)
    assert pages["n"] == 2                                    # fetched again, not read from a save file
