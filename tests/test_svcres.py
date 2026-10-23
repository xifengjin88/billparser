from pathlib import Path

import pytest

from svcres.catalog import Catalog, build
from svcres.resolver import Resolver

RAW = Path(__file__).parent / "fixtures" / "raw"


@pytest.fixture
def resolver(tmp_path):
    cat = build(RAW)
    cat.save(tmp_path / "catalog.msgpack")
    return Resolver(Catalog.load(tmp_path / "catalog.msgpack"), tmp_path)


def test_azure_product_code_exact_and_case_insensitive(resolver):
    for raw in ("DZH318Z0BQ4L", " dzh318z0bq4l "):
        r = resolver.resolve(raw.strip())
        assert r.service == "Virtual Machines" and r.method == "code"


def test_aws_alias_and_service_code(resolver):
    assert resolver.resolve("EC2-Other").service == "Amazon Elastic Compute Cloud"
    assert resolver.resolve("AmazonS3").service == "Amazon Simple Storage Service"


def test_azure_product_name_maps_to_service(resolver):
    r = resolver.resolve("Premium SSD Managed Disks")
    assert r.service == "Storage" and r.category == "Storage"


def test_fuzzy_accepts_close_match(resolver):
    r = resolver.resolve("Big Query")
    assert r.service == "BigQuery" and r.method == "fuzzy"


def test_unknown_returns_candidates_not_guess(resolver):
    r = resolver.resolve("SQL DB")
    assert r.service_key is None
    assert r.candidates


def test_synonym_correction_takes_effect(resolver):
    resolver.add_synonym("SQL DB", "azure:SQL Database", provider="azure")
    assert resolver.resolve("SQL DB", "azure").service == "SQL Database"


def test_dominant_provider_inference(resolver):
    vals = ["Virtual Machines", "Bandwidth", "Azure Cosmos DB", "DZH318Z0BQ1C", "Log Analytics"]
    out = resolver.resolve_many(vals)
    assert Resolver.dominant_provider(out.values()) == "azure"


def test_fuzzy_results_are_cached(resolver):
    resolver.resolve_many(["Big Query"])
    assert resolver.resolve("Big Query").method.startswith("cache:")
