"""Cloud regions: the names and codes that bills print for AWS, Azure and GCP regions.

A label is a region only when the whole label is a known region name or code ("Canada (Central)", "ca-central-1",
"East US 2", "eastus2", "us-central1"). Text that only contains a region name ("$0.05 per GB - US West (Oregon)") is
not a region heading. Matching ignores case and punctuation (see normalize.norm), so "US East (N.Virginia)" and
"US East (N. Virginia)" are the same.
"""

from __future__ import annotations

from .normalize import norm

# code: printed names. AWS bills use the pricing names ("EU (Ireland)"); the console uses "Europe (Ireland)".
AWS = {
    "us-east-1": ["US East (N. Virginia)", "US East (Northern Virginia)"],
    "us-east-2": ["US East (Ohio)"],
    "us-west-1": ["US West (N. California)", "US West (Northern California)"],
    "us-west-2": ["US West (Oregon)"],
    "af-south-1": ["Africa (Cape Town)"],
    "ap-east-1": ["Asia Pacific (Hong Kong)"],
    "ap-east-2": ["Asia Pacific (Taipei)"],
    "ap-south-1": ["Asia Pacific (Mumbai)"],
    "ap-south-2": ["Asia Pacific (Hyderabad)"],
    "ap-southeast-1": ["Asia Pacific (Singapore)"],
    "ap-southeast-2": ["Asia Pacific (Sydney)"],
    "ap-southeast-3": ["Asia Pacific (Jakarta)"],
    "ap-southeast-4": ["Asia Pacific (Melbourne)"],
    "ap-southeast-5": ["Asia Pacific (Malaysia)"],
    "ap-southeast-6": ["Asia Pacific (New Zealand)"],
    "ap-southeast-7": ["Asia Pacific (Thailand)"],
    "ap-northeast-1": ["Asia Pacific (Tokyo)"],
    "ap-northeast-2": ["Asia Pacific (Seoul)"],
    "ap-northeast-3": ["Asia Pacific (Osaka)", "Asia Pacific (Osaka-Local)"],
    "ca-central-1": ["Canada (Central)"],
    "ca-west-1": ["Canada West (Calgary)"],
    "eu-central-1": ["EU (Frankfurt)", "Europe (Frankfurt)"],
    "eu-central-2": ["EU (Zurich)", "Europe (Zurich)"],
    "eu-west-1": ["EU (Ireland)", "Europe (Ireland)", "EU (Dublin)"],
    "eu-west-2": ["EU (London)", "Europe (London)"],
    "eu-west-3": ["EU (Paris)", "Europe (Paris)"],
    "eu-south-1": ["EU (Milan)", "Europe (Milan)"],
    "eu-south-2": ["EU (Spain)", "Europe (Spain)"],
    "eu-north-1": ["EU (Stockholm)", "Europe (Stockholm)"],
    "il-central-1": ["Israel (Tel Aviv)"],
    "me-south-1": ["Middle East (Bahrain)"],
    "me-central-1": ["Middle East (UAE)"],
    "mx-central-1": ["Mexico (Central)"],
    "sa-east-1": ["South America (Sao Paulo)", "South America (São Paulo)"],
    "us-gov-west-1": ["AWS GovCloud (US-West)", "AWS GovCloud (US)"],
    "us-gov-east-1": ["AWS GovCloud (US-East)"],
    "cn-north-1": ["China (Beijing)"],
    "cn-northwest-1": ["China (Ningxia)"],
    "global": ["Global", "Any"],                # services with no region (Support, Route 53, WAF): AWS prints these
}

# code: display name, then the older meter-region names that cost exports print ("US East 2", "EU West")
AZURE = {
    "eastus": ["East US", "US East"], "eastus2": ["East US 2", "US East 2"], "westus": ["West US", "US West"],
    "westus2": ["West US 2", "US West 2"], "westus3": ["West US 3", "US West 3"],
    "centralus": ["Central US", "US Central"], "northcentralus": ["North Central US", "US North Central"],
    "southcentralus": ["South Central US", "US South Central"], "westcentralus": ["West Central US", "US West Central"],
    "canadacentral": ["Canada Central", "CA Central"], "canadaeast": ["Canada East", "CA East"],
    "brazilsouth": ["Brazil South", "BR South"], "brazilsoutheast": ["Brazil Southeast", "BR Southeast"],
    "mexicocentral": ["Mexico Central"], "chilecentral": ["Chile Central"],
    "northeurope": ["North Europe", "EU North"], "westeurope": ["West Europe", "EU West"],
    "uksouth": ["UK South"], "ukwest": ["UK West"],
    "francecentral": ["France Central", "FR Central"], "francesouth": ["France South", "FR South"],
    "germanywestcentral": ["Germany West Central", "DE West Central"], "germanynorth": ["Germany North", "DE North"],
    "switzerlandnorth": ["Switzerland North", "CH North"], "switzerlandwest": ["Switzerland West", "CH West"],
    "norwayeast": ["Norway East", "NO East"], "norwaywest": ["Norway West", "NO West"],
    "swedencentral": ["Sweden Central", "SE Central"], "polandcentral": ["Poland Central", "PL Central"],
    "italynorth": ["Italy North", "IT North"], "spaincentral": ["Spain Central", "ES Central"],
    "eastasia": ["East Asia", "AP East"], "southeastasia": ["Southeast Asia", "AP Southeast"],
    "japaneast": ["Japan East", "JA East"], "japanwest": ["Japan West", "JA West"],
    "koreacentral": ["Korea Central", "KR Central"], "koreasouth": ["Korea South", "KR South"],
    "australiaeast": ["Australia East", "AU East"], "australiasoutheast": ["Australia Southeast", "AU Southeast"],
    "australiacentral": ["Australia Central", "AU Central"], "australiacentral2": ["Australia Central 2", "AU Central 2"],
    "centralindia": ["Central India", "IN Central"], "southindia": ["South India", "IN South"],
    "westindia": ["West India", "IN West"], "uaenorth": ["UAE North", "AE North"], "uaecentral": ["UAE Central", "AE Central"],
    "southafricanorth": ["South Africa North", "ZA North"], "southafricawest": ["South Africa West", "ZA West"],
    "qatarcentral": ["Qatar Central", "QA Central"], "israelcentral": ["Israel Central", "IL Central"],
    "newzealandnorth": ["New Zealand North"], "indonesiacentral": ["Indonesia Central"],
    "malaysiawest": ["Malaysia West"],
}

# GCP bills print the code ("us-central1"), sometimes with the place ("us-central1 (Iowa)"); a place name alone
# ("London") is too common a word to be a region heading.
GCP = {
    "us-central1": "Iowa", "us-east1": "South Carolina", "us-east4": "Northern Virginia", "us-east5": "Columbus",
    "us-west1": "Oregon", "us-west2": "Los Angeles", "us-west3": "Salt Lake City", "us-west4": "Las Vegas",
    "us-south1": "Dallas", "northamerica-northeast1": "Montreal", "northamerica-northeast2": "Toronto",
    "northamerica-south1": "Queretaro", "southamerica-east1": "Sao Paulo", "southamerica-west1": "Santiago",
    "europe-west1": "Belgium", "europe-west2": "London", "europe-west3": "Frankfurt", "europe-west4": "Netherlands",
    "europe-west6": "Zurich", "europe-west8": "Milan", "europe-west9": "Paris", "europe-west10": "Berlin",
    "europe-west12": "Turin", "europe-north1": "Finland", "europe-north2": "Stockholm", "europe-central2": "Warsaw",
    "europe-southwest1": "Madrid", "asia-east1": "Taiwan", "asia-east2": "Hong Kong", "asia-northeast1": "Tokyo",
    "asia-northeast2": "Osaka", "asia-northeast3": "Seoul", "asia-south1": "Mumbai", "asia-south2": "Delhi",
    "asia-southeast1": "Singapore", "asia-southeast2": "Jakarta", "australia-southeast1": "Sydney",
    "australia-southeast2": "Melbourne", "me-west1": "Tel Aviv", "me-central1": "Doha", "me-central2": "Dammam",
    "africa-south1": "Johannesburg",
}


def _index() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for code, names in AWS.items():
        for n in [code, *names]:
            out.setdefault(norm(n), ("aws", code))
    for code, names in AZURE.items():
        for n in [code, *names]:
            out.setdefault(norm(n), ("azure", code))
    for code, place in GCP.items():
        for n in (code, f"{code} ({place})", f"{place} ({code})"):
            out.setdefault(norm(n), ("gcp", code))
    return out


INDEX = _index()


def lookup(label: object) -> tuple[str, str] | None:
    """(provider, region code) when the whole label is a region name or code, else None."""
    return INDEX.get(norm(label)) if label is not None else None
