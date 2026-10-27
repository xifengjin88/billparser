"""The price catalog (task 05): load the AWS price files into the price tables with history, and look prices up.

The fixture is cut from the public AWS price files (tests/fixtures/prices/make_fixture.py): us-east-1 m5.xlarge
Linux (plain, SQL Std, Ent and Web, an unused capacity reservation), m5.xlarge Windows (license included, BYOL, license
billed apart, SQL Std), AWSOutposts' m5.xlarge, RDS MySQL db.m5.large, an S3 price with tiers, and the Compute Savings
Plans rates of the Linux m5.xlarge.
"""

from __future__ import annotations

import csv
import gzip
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa

from billparse.attributes import extract
from billparse.db import engine_for
from prices import normalize as nz
from prices.load import Refused, load_snapshot
from prices.store import PriceCatalog

FIXTURE = Path(__file__).parent / "fixtures" / "prices" / "download"
LINUX = "5G4TA8Z4MUKE6MJB"              # m5.xlarge Linux Shared, Used: $0.192 per hour
LINUX_OD = "5G4TA8Z4MUKE6MJB.JRTCKXETXF.6YS6EN2CT7"


@pytest.fixture
def engine(tmp_path):
    return engine_for(f"sqlite:///{tmp_path / 'prices.db'}")


@pytest.fixture
def catalog(engine):
    load_snapshot(engine, FIXTURE)
    return PriceCatalog(engine)


def _edit(download: Path, offer: str, change) -> None:
    """Rewrite the one file of a price list in a copy of the fixture: change(row) -> row, or None to drop it."""
    (path,) = (download / "offers" / offer).glob("*/us-east-1.csv.gz")
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        head = [fh.readline() for _ in range(5)]
        reader = csv.DictReader(fh)
        names, rows = reader.fieldnames, [r for r in (change(dict(r)) for r in reader) if r is not None]
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        fh.writelines(head)
        w = csv.DictWriter(fh, names, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)


def _rate_codes(download: Path, offer: str, where) -> list[str]:
    (path,) = (download / "offers" / offer).glob("*/us-east-1.csv.gz")
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        for _ in range(5):
            fh.readline()
        return sorted(r["RateCode"] for r in csv.DictReader(fh) if where(r))


def _keys(**cols) -> pd.DataFrame:
    k = pd.DataFrame(cols)
    k["key_id"] = range(len(k))
    return k


def _one(found: pd.DataFrame, key_id: int = 0) -> dict:
    return found.set_index("key_id").loc[key_id].to_dict()


def test_load_counts_and_snapshot(engine):
    res = load_snapshot(engine, FIXTURE)
    assert res.status == "current" and res.snapshot_id == "2026-10-27"
    assert res.products == 14 and res.rates == 145 and res.sp_rates == 12
    assert PriceCatalog(engine).version == "2026-10-27"


def test_small_batches_load_the_same_rows(engine, tmp_path):
    load_snapshot(engine, FIXTURE)
    small = engine_for(f"sqlite:///{tmp_path / 'small.db'}")
    load_snapshot(small, FIXTURE, block_bytes=4096)          # a product's rows over several batches
    for table, cols in (("price_product", "sku, attr_hash, attributes"),
                        ("price_rate", "rate_code, unit_price, effective_from"),
                        ("price_sp_rate", "plan_sku, discounted_sku, rate")):
        sql = f"SELECT {cols} FROM {table} ORDER BY 1, 2"
        with engine.connect() as a, small.connect() as b:
            assert a.exec_driver_sql(sql).all() == b.exec_driver_sql(sql).all(), table


def test_a_second_load_of_the_same_files_writes_nothing(engine):
    load_snapshot(engine, FIXTURE)
    with engine.connect() as conn:
        before = conn.exec_driver_sql("SELECT count(*) FROM price_rate").scalar_one()
    res = load_snapshot(engine, FIXTURE, snapshot_id="2026-11-03")
    assert (res.closed, res.inserted) == (0, 0)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM price_rate").scalar_one() == before
        assert conn.exec_driver_sql("SELECT status FROM price_snapshot ORDER BY id").scalars().all() == [
            "previous", "current"]


def test_rate_code_and_sku(catalog):
    found = catalog.lookup(_keys(as_of=["2026-10-27", "2026-10-27"], rate_code=[LINUX_OD, None], sku=[None, LINUX]))
    for key_id in (0, 1):
        row = _one(found, key_id)
        assert Decimal(str(row["unit_price"])) == Decimal("0.192") and row["unit"] == "hour"
        assert row["rate_code"] == LINUX_OD and row["candidates"] == 1
    assert _one(found, 0)["matched_by"] == "rate_code" and _one(found, 1)["matched_by"] == "sku"


def test_reserved_terms_by_sku(catalog):
    found = catalog.lookup(_keys(as_of=["2026-10-27"] * 2, sku=[LINUX] * 2, term_type=["reserved"] * 2,
                                 lease_length=["1 year"] * 2, purchase_option=["No Upfront"] * 2,
                                 offering_class=["Standard", None]))
    row = _one(found, 0)
    assert row["term_type"] == "reserved" and row["unit"] == "hour" and 0 < row["unit_price"] < 0.192
    assert _one(found, 1)["candidates"] == 2                    # standard and convertible: the class is needed


def test_usage_type_capacity_reservations_and_outposts(catalog):
    found = catalog.lookup(_keys(
        as_of=["2026-10-27"] * 4, usage_type=["BoxUsage:m5.xlarge", "UnusedBox:m5.xlarge", "BoxUsage:m5.xlarge"] * 1
        + ["BoxUsage:m5.xlarge"], operation=["RunInstances"] * 4, region=["us-east-1"] * 4,
        service_code=[None, None, "AmazonEC2", "AWSOutposts"]))
    assert _one(found, 0)["sku"] == LINUX                       # not the Outpost product of the same usage type
    assert _one(found, 1)["sku"] == "E9RWRGDUV5X76FUB"          # the unused capacity reservation's own usage type
    assert _one(found, 2)["sku"] == LINUX
    assert _one(found, 3)["sku"] == "WYFNYKVVU5FD662T"          # the line's product code chooses the Outpost


def test_attributes(catalog):
    found = catalog.lookup(_keys(
        as_of=["2026-10-27"] * 4, service_code=["AmazonEC2"] * 4, instance_type=["m5.xlarge"] * 4,
        region=["US East (N. Virginia)"] * 4, operating_system=["Linux", "Windows", "Windows", None],
        tenancy=["Shared", "Shared", "Shared", None], pre_installed_sw=["NA", "SQL Std", "SQL Std", None],
        license_model=[None, "No License required", None, None]))
    assert _one(found, 0)["sku"] == LINUX and _one(found, 0)["matched_by"] == "attributes"
    assert _one(found, 1)["sku"] == "XX43P9XZN7A2HHHK" and _one(found, 1)["unit_price"] > 0.192
    for key_id in (2, 3):                                       # several prices fit: none is chosen
        row = _one(found, key_id)
        assert row["candidates"] > 1 and pd.isna(row["unit_price"])


def test_invoice_attributes_without_software_or_license_are_ambiguous(catalog):
    found = catalog.lookup(_keys(
        as_of=["2026-10-27"] * 2, service_code=["AmazonEC2"] * 2, instance_type=["m5.xlarge"] * 2,
        region=["us-east-1"] * 2, operating_system=["Linux", "Windows"], tenancy=["Shared"] * 2,
        license_model=["No License required", None], pre_installed_sw=[None, "NA"]))
    linux, windows = _one(found, 0), _one(found, 1)
    assert linux["candidates"] == 4 and pd.isna(linux["unit_price"])        # plain, SQL Std, SQL Ent, SQL Web
    # included $0.376, BYOL and billed apart (:box) both $0.192: candidates counts distinct prices
    assert windows["candidates"] == 2 and pd.isna(windows["unit_price"])


INVOICE = [   # what an invoice or a Cost Explorer file prints: words, no SKU, no software or license column
    ("$0.192 per On Demand Linux m5.xlarge Instance Hour", None, "5G4TA8Z4MUKE6MJB"),
    ("$0.672 per On Demand Linux with SQL Std m5.xlarge Instance Hour", None, "EHFDZFP2EH9BD4DX"),
    ("$0.376 per On Demand Windows m5.xlarge Instance Hour", None, "HXGQ45BFM9J2FJ3F"),
    ("$0.192 per On Demand Windows BYOL m5.xlarge Instance Hour", None, "8AW2DYRV3M9N5RRZ"),
    ("Amazon EC2 m5.xlarge", "RunInstances:0200", "TSKNSGPA2KR5BHCN"),             # the operation code: SQL Web
    ("Amazon EC2 m5.xlarge", "RunInstances:0002:box", "AWJMJ73C9RDQFD5N"),         # Windows, license billed apart
]


def test_parsed_invoice_lines_get_a_price(catalog):
    rows = pd.DataFrame({"description": [d for d, _, _ in INVOICE], "operation": [o for _, o, _ in INVOICE],
                         "charge_type": "Usage"})
    extract(rows)
    assert rows["pre_installed_sw"].tolist() == ["NA", "SQL Std", "NA", "NA", "SQL Web", "NA"]
    assert rows["tenancy"].tolist() == ["Shared"] * 6
    assert rows["_how:pre_installed_sw"].tolist() == ["rule", "text", "rule", "rule", "rule", "rule"]
    keys = rows.reindex(columns=["instance_type", "operating_system", "pre_installed_sw", "license_model", "tenancy",
                                 "database_engine"]).assign(key_id=range(len(rows)), as_of="2026-10-27", service_code="AmazonEC2",
                                            region="us-east-1")
    found = catalog.lookup(keys)
    assert [_one(found, i)["sku"] for i in range(len(INVOICE))] == [sku for _, _, sku in INVOICE]
    assert (found["candidates"] == 1).all()


def test_a_printed_value_is_never_replaced(catalog):
    rows = pd.DataFrame({"description": ["$0.192 per On Demand Windows m5.xlarge Instance Hour"],
                         "pre_installed_sw": ["SQL Std"], "tenancy": ["Dedicated"], "charge_type": ["Usage"]})
    extract(rows)
    assert (rows.loc[0, "pre_installed_sw"], rows.loc[0, "tenancy"]) == ("SQL Std", "Dedicated")


def test_rds_and_other_lines_get_no_ec2_software():
    rows = pd.DataFrame({"description": ["$0.171 per RDS db.m5.large Single-AZ instance hour running SQL Server Std",
                                         "$0.09 per GB - Linux data transfer out"],
                         "charge_type": ["Usage", "Usage"]})
    extract(rows)
    assert "pre_installed_sw" not in rows or rows["pre_installed_sw"].isna().all()
    assert "tenancy" not in rows or rows["tenancy"].isna().all()


def test_lookup_types_are_the_same_on_every_database(catalog):
    found = catalog.lookup(_keys(as_of=["2025-01-01", "2026-10-27"], sku=[LINUX, "WP9ANXZGBYYSGJEA"]))
    assert found["unit_price"].dtype == float and found["before_history"].dtype == bool
    assert found["tiered"].dtype == bool and found["candidates"].dtype == int
    sp = catalog.lookup_savings_plans(_keys(as_of=["2026-10-27"], discounted_sku=[LINUX]))
    assert sp["rate"].dtype == float and sp["before_history"].dtype == bool
    assert isinstance(2.0 * found["unit_price"].iloc[0], float)


def test_rds(catalog):
    found = catalog.lookup(_keys(as_of=["2026-10-27"], service_code=["AmazonRDS"], instance_type=["db.m5.large"],
                                 region=["us-east-1"],
                                 database_engine=["MySQL"], deployment_option=["Single-AZ"]))
    assert _one(found)["sku"] == "C86H957YE4QF4727"            # not the unused reservation of the same size


def test_tiers(catalog):
    row = _one(catalog.lookup(_keys(as_of=["2026-10-27"], sku=["WP9ANXZGBYYSGJEA"])))
    assert row["tiered"] and Decimal(str(row["unit_price"])) == Decimal("0.023") and row["unit"] == "gb_month"


def test_savings_plans(catalog):
    every = catalog.lookup_savings_plans(_keys(as_of=["2026-10-27"], discounted_sku=[LINUX]))
    assert len(every) == 12 and set(every["plan_type"]) == {"ComputeSavingsPlans", "EC2InstanceSavingsPlans"}
    one = catalog.lookup_savings_plans(_keys(as_of=["2026-10-27"], discounted_sku=[LINUX],
                                             plan_type=["ComputeSavingsPlans"], lease_length=["1 year"],
                                             purchase_option=["No Upfront"]))
    assert len(one) == 1 and 0 < one["rate"].iloc[0] < 0.192


def test_price_change_keeps_history(engine, tmp_path):
    load_snapshot(engine, FIXTURE)
    download = tmp_path / "download"
    shutil.copytree(FIXTURE, download)

    def raise_linux(r: dict) -> dict:
        if r["RateCode"] == LINUX_OD:
            r["PricePerUnit"], r["EffectiveDate"] = "0.2000000000", "2026-11-01T00:00:00Z"
        return r
    _edit(download, "AmazonEC2", raise_linux)
    res = load_snapshot(engine, download, snapshot_id="2026-11-03")
    assert (res.closed, res.inserted) == (1, 1)
    cat = PriceCatalog(engine)
    found = cat.lookup(_keys(as_of=["2025-01-01", "2026-10-31", "2026-11-01"], rate_code=[LINUX_OD] * 3))
    old, before_new, new = (_one(found, i) for i in range(3))
    assert old["unit_price"] == pytest.approx(0.192) and old["before_history"]   # the oldest price the catalog has
    assert before_new["unit_price"] == pytest.approx(0.192) and not before_new["before_history"]
    assert new["unit_price"] == pytest.approx(0.2)


def test_a_removed_price_is_closed(engine, tmp_path):
    load_snapshot(engine, FIXTURE)
    download = tmp_path / "download"
    shutil.copytree(FIXTURE, download)
    (gone,) = _rate_codes(download, "AmazonEC2", lambda r: r["TermType"] == "Reserved")[:1]
    _edit(download, "AmazonEC2", lambda r: None if r["RateCode"] == gone else r)     # 1 rate: under the 10% limit
    res = load_snapshot(engine, download, snapshot_id="2026-11-03", partial=True, only=["AmazonEC2"])
    assert (res.closed, res.inserted) == (1, 0)
    with engine.connect() as conn:
        closed = conn.exec_driver_sql("SELECT rate_code, effective_to FROM price_rate WHERE effective_to IS NOT NULL"
                                      ).all()
    assert [tuple(r) for r in closed] == [(gone, "2026-11-03")]


def test_sanity_check_refuses_a_big_drop(engine, tmp_path):
    load_snapshot(engine, FIXTURE)
    download = tmp_path / "download"
    shutil.copytree(FIXTURE, download)
    _edit(download, "AmazonRDS", lambda r: None if r["SKU"] == "C86H957YE4QF4727" else r)    # 10 of 11 rates
    with pytest.raises(Refused, match="AmazonRDS"):
        load_snapshot(engine, download, snapshot_id="2026-11-03")
    with engine.connect() as conn:
        snaps = dict(conn.exec_driver_sql("SELECT id, status FROM price_snapshot").all())
    assert snaps == {"2026-10-27": "current", "2026-11-03": "refused"}


def test_a_full_load_refuses_a_missing_price_list(engine, tmp_path):
    load_snapshot(engine, FIXTURE)
    download = tmp_path / "download"
    shutil.copytree(FIXTURE, download)
    manifest = json.loads((download / "manifest.json").read_text())
    manifest["files"] = {k: v for k, v in manifest["files"].items() if not k.startswith("AmazonS3")}
    (download / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(Refused, match="AmazonS3 is missing"):
        load_snapshot(engine, download, snapshot_id="2026-11-03")
    load_snapshot(engine, download, snapshot_id="2026-11-03", partial=True)     # a partial load may miss it


def test_unknown_keys_find_nothing(catalog):
    assert catalog.lookup(_keys(as_of=["2026-10-27"], sku=["NOPE"], usage_type=["Nope:x"])).empty


@pytest.mark.parametrize(("raw", "term"), [("1 year", "1yr"), ("3 year", "3yr"), ("3 years", "3yr"), ("36 months", "3yr"),
                                           ("P1Y", "1yr"), ("3yr", "3yr"), (None, None)])
def test_term(raw, term):
    assert nz.term(raw) == term


def test_tables_exist_in_sqlite(engine):
    names = set(sa.inspect(engine).get_table_names())
    assert {"price_snapshot", "price_product", "price_rate", "price_sp_plan", "price_sp_rate"} <= names
