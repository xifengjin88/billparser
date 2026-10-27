"""Attribute values spelled one way, for the catalog import and for task 06 on the parsed lines: both sides of a
match must agree. Case and spacing are normalized and known synonyms mapped; an unknown value is kept as it is (trimmed),
never emptied."""

from __future__ import annotations

import re

from svcres.regions import lookup as region_lookup

# the AWS price list's names
OPERATING_SYSTEMS = {"linux": "Linux", "linux/unix": "Linux", "unix/linux": "Linux", "amazon linux": "Linux",
                     "windows": "Windows", "windows server": "Windows", "rhel": "RHEL",
                     "red hat enterprise linux": "RHEL", "red hat": "RHEL", "suse": "SUSE", "suse linux": "SUSE",
                     "sles": "SUSE", "red hat enterprise linux with ha": "Red Hat Enterprise Linux with HA",
                     "rhel with ha": "Red Hat Enterprise Linux with HA", "ubuntu pro": "Ubuntu Pro"}
TENANCIES = {"shared": "Shared", "default": "Shared", "dedicated": "Dedicated", "host": "Host"}
LICENSE_MODELS = {"no license required": "No License required", "license included": "License Included",
                  "bring your own license": "Bring your own license", "byol": "Bring your own license"}
PRE_INSTALLED_SW = {"na": "NA", "none": "NA", "sql std": "SQL Std", "sql standard": "SQL Std", "sql web": "SQL Web",
                    "sql ent": "SQL Ent", "sql enterprise": "SQL Ent", "sql server standard": "SQL Std",
                    "sql server web": "SQL Web", "sql server enterprise": "SQL Ent"}
CAPACITY = {"used": "Used", "unusedcapacityreservation": "UnusedCapacityReservation",
            "allocatedcapacityreservation": "AllocatedCapacityReservation"}
PRICING_MODELS = {"ondemand": "on_demand", "on_demand": "on_demand", "reserved": "reserved", "ri": "reserved",
                  "reservation": "reserved", "savingsplan": "savings_plan", "savings_plan": "savings_plan",
                  "committeduse": "committed_use", "committed_use": "committed_use", "cud": "committed_use",
                  "spot": "spot"}
TERMS = {"1yr": "1yr", "1 yr": "1yr", "1 year": "1yr", "1year": "1yr", "12 months": "1yr", "p1y": "1yr",
         "3yr": "3yr", "3 yr": "3yr", "3 years": "3yr", "3year": "3yr", "36 months": "3yr", "p3y": "3yr"}
PAYMENT_OPTIONS = {"no upfront": "no_upfront", "partial upfront": "partial_upfront", "all upfront": "all_upfront",
                   "no_upfront": "no_upfront", "partial_upfront": "partial_upfront", "all_upfront": "all_upfront"}


def _key(v: object) -> str | None:
    if v is None or (isinstance(v, float) and v != v):
        return None
    s = re.sub(r"\s+", " ", str(v).strip())
    return s or None


def _mapped(table: dict[str, str], v: object, squash: bool = False) -> str | None:
    s = _key(v)
    if s is None:
        return None
    k = s.lower().replace(" ", "") if squash else s.lower()
    return table.get(k, s)


def operating_system(v: object) -> str | None:
    return _mapped(OPERATING_SYSTEMS, v)


def tenancy(v: object) -> str | None:
    return _mapped(TENANCIES, v)


def license_model(v: object) -> str | None:
    return _mapped(LICENSE_MODELS, v)


def pre_installed_sw(v: object) -> str | None:
    return _mapped(PRE_INSTALLED_SW, v)


def capacity_status(v: object) -> str | None:
    return _mapped(CAPACITY, v, squash=True)


def pricing_model(v: object) -> str | None:
    s = _key(v)
    return PRICING_MODELS.get(re.sub(r"[\s-]+", "", s.lower()), s) if s else None


def term(v: object) -> str | None:
    """1yr | 3yr from "1 year", "3 years", "3yr", "36 months", "P3Y"; anything else as printed."""
    s = _key(v)
    if s is None:
        return None
    m = re.fullmatch(r"p?(\d+)\s*(y|yr|yrs|year|years|m|mo|month|months)", s.lower())
    if not m:
        return TERMS.get(s.lower(), s)
    years = int(m[1]) / (12 if m[2].startswith("m") else 1)
    return f"{years:g}yr"


def payment_option(v: object) -> str | None:
    return _mapped(PAYMENT_OPTIONS, v)


def instance_type(v: object) -> str | None:
    """As printed, trimmed; Azure sizes keep Standard_."""
    return _key(v)


def region(v: object) -> str | None:
    """A region code (us-east-1, eastus, us-central1), from a code or a name ("US East (N. Virginia)", "East US")."""
    s = _key(v)
    if s is None:
        return None
    hit = region_lookup(s)
    return hit[1] if hit else s


NORMALIZERS = {"operating_system": operating_system, "tenancy": tenancy, "license_model": license_model,
               "pre_installed_sw": pre_installed_sw, "capacity_status": capacity_status,
               "pricing_model": pricing_model, "term": term, "payment_option": payment_option,
               "instance_type": instance_type, "region": region}
