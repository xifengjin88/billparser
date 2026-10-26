---
type: concept
title: Service resolution, attributes and analysis
description: How billparse maps line labels and codes to canonical AWS, Azure and GCP services (svcres catalog and resolver), sets charge kinds and areas, and computes the analysis numbers (spend by service, category and kind, period, forecast, commitment-eligible spend).
tags: [svcres, services, catalog, analysis, forecast]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-25T20:19:27.415Z
sources:
  - id: openwiki-source-3b5b4328bbe173efa9023074
    resource: repo://billparse/analyze.py
  - id: openwiki-source-6f8cce9cd1948d0615350074
    resource: repo://scripts/refresh_catalog.sh
  - id: openwiki-source-055c60736d150e0550303686
    resource: repo://svcres/catalog.py
  - id: openwiki-source-e569bc824dcd532376eb07a1
    resource: repo://svcres/resolver.py
generated: { by: "claude-code", at: "2026-10-25T20:19:27.415Z" }
---

# Service resolution, attributes and analysis

After a reading is chosen, the pipeline resolves each line's service (`analyze.resolve_services`) and, for accepted
cost lines, computes the analysis (`analyze.analyze`). Attribute extraction from line text is described in
[Rule mapper and layout fingerprints](../architecture/rule-mapper.md).

## The service catalog (`svcres/catalog.py`)

`Catalog` is four plain dicts, saved as msgpack (`data/catalog.msgpack`) and loaded once per process:

| Dict | Key → value |
|---|---|
| `services` | `provider:Name` → `{provider, name, category, family, code}` |
| `codes` | `provider:CODE` → service key (AWS service codes, Azure product and service ids, GCP service ids) |
| `names` | `provider:normalized name` → service key (canonical names and aliases; small) |
| `products` | `provider:normalized name` → service key (Azure product names; large) |

`build(raw_dir)` reads `data/raw/azure_products.json`, `aws_services.json` and `gcp_services.json`, adds hand-kept
seeds (`svcres/seeds.py`: shared categories, AWS categories and aliases such as "EC2-Other", Azure aliases, GCP
keyword categories, Marketplace sellers), and one `Marketplace` service per cloud. Missing raw files are skipped. The
repository's raw files have 272 AWS services, 1,498 Azure products and only 6 GCP services, so GCP labels resolve
poorly until the GCP list is filled.

`scripts/build_catalog.py` builds the file; `scripts/refresh_catalog.sh` fetches, builds to a new file, refuses a
catalog whose code count dropped by more than 10%, and then swaps it in. If no catalog file exists,
`analyze.get_resolver` builds one from `data/raw` on first use.

## The resolver (`svcres/resolver.py`)

`Resolver.resolve(raw, provider)` stops at the first hit:

1. **cache** of earlier fuzzy, prefix and fallback picks (`resolution_cache.json`), cleared when the catalog is
   rebuilt;
2. **code** exact match;
3. **synonym** (`synonyms.json`, learned from people's corrections; provider-specific first, then global);
4. **name**: exact normalized name, then the loose name (filler words removed). A name that matches services in two
   providers ("Kubernetes") is `ambiguous` and returns candidates instead of a guess;
5. **synonym prefix** (a person's alias as the first words of a label), **name prefix** (longest known word prefix:
   "Amazon EC2 m5.xlarge" → EC2), and **Azure abbreviations** ("Az DDOS Prot-…" → Azure DDoS Protection);
6. **fuzzy** (RapidFuzz `WRatio`; accepted at 92 or more, candidates from 70), on the small name list first and then
   on Azure product names with a cheap prefilter;
7. an optional **fallback** hook that may only choose from the fuzzy candidates.

An unresolved label keeps up to 5 candidates for the review screen. `add_synonym` records a correction, clears
affected cache entries and takes effect at once.

`resolve_many(values, provider)` resolves unique values; without a provider it retries unresolved values with the
file's dominant provider (80% or more of the confident matches, at least 3).

## Resolving a file's lines (`analyze.resolve_services`)

- The resolver is shared by all parse threads and guarded by `RESOLVER_LOCK`.
- A provider hint is used when one provider is more than 90% of the `provider` column.
- Columns are tried in order: `product_code`, `service_raw`, `description`; each fills only lines still unresolved.
- Without a provider column, `_one_provider` re-checks weak (prefix or fuzzy) matches to other providers when 80% of
  exact matches are one provider's.
- Rows get `service`, `service_name`, `service_code`, `category`, `resolved_by` and `provider`. An unresolved line
  keeps its raw label as `service`.
- `_fees_and_areas`: a Support-category line becomes charge type `Support`; a program fee (for example the APN fee,
  `FEE_SERVICES`) becomes `Fee`; a line marked as sold through a Marketplace gets the category `Marketplace`; a
  commitment purchase with no service gets the area its name says ("Compute savings plan" → Compute).

The return value is the **resolution share**: the share of usage cost (not tax, credit, purchase, fee, support or
adjustment) whose service was resolved. Validation uses it as the service resolution check.

## The analysis (`analyze.analyze`)

Computed only for accepted cost lines:

| Key | Content |
|---|---|
| `total_billed`, `usage_spend` | all lines; lines whose charge type is not in `NON_USAGE` |
| `non_usage` | money per non-usage charge type (Tax, Credit, Refund, Purchase, Fee, Support, Adjustment) |
| `by_provider`, `by_category`, `top_services` | usage spend groupings (top 15 services with shares) |
| `by_service_and_type`, `by_category_and_type` | each service's or area's money by kind (usage, tax, credit, commitment fee, support fee, fee, marketplace) |
| `period` | from line dates (CUR-style exclusive end dates handled) or an invoice header field |
| `monthly` | usage per month when lines have dates |
| `forecast` | next month and next quarter as low / expected / high |
| `commitment_opportunity` | eligible spend and an indicative savings range |

**Forecast.** With 2+ months of data, a damped trend (median monthly growth clipped to ±20%, then halved; the last
month is treated as partial when there are more than 2); with fewer than 27 days, a run rate; else the bill's usage.
The range widens with lower confidence, a status other than `reconciled`, and short history.

**Commitment opportunity.** Spend in Compute, Databases, Containers and Analytics is "eligible"; a baseline coverage
range (higher when monthly spend is stable), and `indicative_monthly_savings` from an assumed 20–40% discount. This
does not know which usage is already covered by commitments; see `docs/new_direction/01-cost-types-and-coverage.md`.
