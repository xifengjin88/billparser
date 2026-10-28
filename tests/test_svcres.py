import json
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


def test_exact_name_wins_over_a_loose_name(tmp_path):
    """"Microsoft Fabric" is its own service: the loose key of "Service Fabric" (prefixes removed) must not tie it."""
    raw = tmp_path / "raw"
    raw.mkdir()
    for f in ("aws_services.json", "gcp_services.json"):
        (raw / f).write_text((RAW / f).read_text())
    rows = [("P1", "Fabric Capacity", "Microsoft Fabric", "Data"), ("P2", "Service Fabric Cluster", "Service Fabric", "Compute")]
    (raw / "azure_products.json").write_text(json.dumps({
        pid: {"productId": pid, "productName": prod, "serviceName": svc, "serviceId": pid + "S", "serviceFamily": fam}
        for pid, prod, svc, fam in rows}))
    cat = build(raw)
    cat.save(tmp_path / "catalog.msgpack")
    res = Resolver(Catalog.load(tmp_path / "catalog.msgpack"), tmp_path)
    assert res.resolve("Microsoft Fabric").service == "Microsoft Fabric"
    assert res.resolve("Service Fabric").service == "Service Fabric"


def test_a_rebuilt_catalog_starts_a_new_cache(tmp_path):
    cat = build(RAW)
    cat.save(tmp_path / "catalog.msgpack")
    res = Resolver(Catalog.load(tmp_path / "catalog.msgpack"), tmp_path)
    res.cache["*|old label"] = {"service_key": "azure:Storage", "method": "fuzzy", "score": 93.0}
    res._dirty = True
    res.flush()
    assert Resolver(Catalog.load(tmp_path / "catalog.msgpack"), tmp_path).resolve("old label").method == "cache:fuzzy"
    cat.built_at += 1                                  # a rebuild
    cat.save(tmp_path / "catalog.msgpack")
    assert Resolver(Catalog.load(tmp_path / "catalog.msgpack"), tmp_path).resolve("old label").method != "cache:fuzzy"


def test_a_resolved_service_has_its_code_and_name(resolver):
    aws = resolver.resolve("EC2-Other")
    assert (aws.service_code, aws.service) == ("AmazonEC2", "Amazon Elastic Compute Cloud")
    azure = resolver.resolve("Premium SSD Managed Disks")
    assert azure.service == "Storage" and azure.service_code                  # the Azure serviceId
    gcp = resolver.resolve("BigQuery")
    assert gcp.service_code == "24E6-581D-38E5"


def test_marketplace_sellers_and_an_alias_as_a_word_prefix(resolver):
    """Each cloud has a Marketplace service; an alias to it covers every product name that starts with the alias."""
    key = resolver.key_for_code("Marketplace", "azure")
    assert key == "azure:Marketplace"
    assert resolver.resolve("Acme Backup Pro 10TB", "azure").service_key is None
    resolver.add_synonym("Acme Backup", key, provider="azure")
    r = resolver.resolve("Acme Backup Pro 10TB", "azure")
    assert (r.service, r.category, r.method) == ("Marketplace", "Marketplace", "synonym-prefix")


def test_a_cache_file_being_written_never_breaks_a_reader(tmp_path):
    """Several workers share resolution_cache.json: a reader never sees half a file, and a bad file is only a miss."""
    import threading

    cat = build(RAW)
    cat.save(tmp_path / "catalog.msgpack")
    catalog = Catalog.load(tmp_path / "catalog.msgpack")
    writer = Resolver(catalog, tmp_path)
    writer.cache.update({f"label {i}": {"service": "x" * 200} for i in range(2000)})
    errors: list[Exception] = []
    stop = threading.Event()

    def write() -> None:
        while not stop.is_set():
            writer._dirty = True
            writer.flush()

    def read() -> None:
        for _ in range(100):
            try:
                Resolver(catalog, tmp_path)
            except Exception as e:  # noqa: BLE001 - the test counts every failure
                errors.append(e)

    t = threading.Thread(target=write)
    t.start()
    readers = [threading.Thread(target=read) for _ in range(3)]
    for r in readers:
        r.start()
    for r in readers:
        r.join()
    stop.set()
    t.join()
    assert errors == []
    assert not list(tmp_path.glob("*.tmp"))

    (tmp_path / "resolution_cache.json").write_text('{"half": ')
    assert Resolver(catalog, tmp_path).resolve("EC2-Other").service == "Amazon Elastic Compute Cloud"
