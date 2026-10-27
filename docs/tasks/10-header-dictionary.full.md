# Task 10: Header dictionary and value-first column detection (self-contained)

This file holds everything you need for this task: the project context first (parts A–D), then the task itself
(part E). It replaces the older `10-header-dictionary.md`.

**Order:** last. Start after task 09 (`task/09-job-queue`) is merged into `main`. **Needs:** 01 and 03 (the
fields), 05 (price catalog, to confirm instance types and SKUs), 08 (structured spec output), 09 (several worker
processes share one database, which is why the learned dictionary lives in PostgreSQL). **STOP gate:** none.

---

## A. What billparse is, and where the mapper fits

billparse reads cloud bills and cost files and returns canonical line items with proof. A file goes through routes,
cheapest first, and stops at the first accepted attempt:

```
_builtin -> _learned -> _mapper -> _llm_spec -> _llm_extract
```

- **Built-in specs** read known provider exports (CUR, FOCUS, Azure and GCP exports).
- **Learned specs** read layouts seen before.
- **The rule mapper** (`billparse/mapper.py`) is the last free route. It guesses which column holds which field from
  header words and what the values look like, then builds a spec.
- Everything after the mapper calls Claude and costs money.

Customers upload their own spreadsheets, reseller exports and non-English invoices, so every column the mapper reads
correctly is an LLM call saved, and every cost column it misses is a price check that cannot run.

## B. What exists today (verified on `main` at 88e4edb, task 08 merged)

- **`mapper.HEADER_RULES`:** hard-coded English regexes per canonical field, with weights.
  - `header_score(field, header)` splits camelCase, normalizes with `layout.norm_h`, takes the best rule weight,
    and subtracts 2.0 when the field's `NEGATIVE` regex also matches.
- **`mapper.value_score(field, Profile)`:** checks the values (numeric, date, code-like, letters, distinct share)
  and adds a bonus or a −5 penalty.
- **`mapper.map_columns(columns)`:** for each column and field, `if hs <= 0: continue`. **A column is considered for
  a field only when its header matches a rule.** `value_score` only adjusts the score; it never proposes a field.
  - So a column of `m5.xlarge` values under an unknown or foreign header is never mapped.
  - Pairs are taken greedily by score (minimum 2.0). If no cost column was found, the rightmost money-looking
    column becomes `billed_cost`.
- **22 of the 45 canonical fields (`transforms.CANONICAL`) have no header rule:**
  - costs and prices: `list_cost`, `effective_cost`, `contracted_cost`, `list_unit_price`,
    `contracted_unit_price`, `credits`;
  - identifiers: `resource_id`, `sku_id`, `sku_price_id`, `meter_id`, `operation`;
  - product attributes: `tenancy`, `license_model`, `pre_installed_sw`;
  - commitments: `purchase_option`, `pricing_term`, `commitment_type`, `commitment_term`, `commitment_status`;
  - other: `provider`, `line_type_raw`, `coverage`.

  A customer's own export with "List Cost", "Amortized Cost", "Resource ID" or "SKU" columns loses them. The price
  checks (task 07) then have nothing to test.
- **`effective price` matches `unit_price`.** On an Azure-style file with both `UnitPrice` and `EffectivePrice`,
  the first one by score wins and the other is dropped.
- **Value helpers that already exist:**
  - `attributes.instance_type(text)` (AWS, Azure and GCP patterns), `attributes.USAGE_TYPE` and
    `attributes.REGION_PREFIX`;
  - `svcres/regions.py` (region names and codes);
  - `prices.store.PriceCatalog.exists(keys)` (task 07): which SKUs, usage types or instance type and region pairs
    the catalog has, in one query;
  - `PriceCatalog.services()`.
- **Learning exists for service labels only.** `svcres.Resolver.add_synonym` writes `CATALOG_DIR/synonyms.json`.
  `review.approve` (from `POST /documents/{sha}/review/approve`) saves corrected service codes as synonyms, and
  promotes the candidate spec to `verified` when no charge changed. Nothing learns column headers.
  `learn.promote_candidates` promotes learned specs in the nightly loop.
- **Without an LLM:** `samples/files/german_invoice.xlsx` goes through the mapper and ends `needs_review`
  (confidence 0.6). The tests make it pass only with an LLM spec (`GERMAN_SPEC` in `tests/test_pipeline.py`). The
  other mapper samples (`custom_api.json`, `custom_workbook.xlsx`, `messy_report.csv`, `spend_optimizer_pivot.xlsx`,
  the two PDFs) are `reconciled`.
- **`learn.metrics`** reports `no_llm_share`: the share of parses with no LLM call. This task should raise it.
- **Task 08:** `llm.generate_spec(llm, preview, examples, feedback, previous, note)` returns a schema-shaped spec. The
  first round runs `spec_candidates` candidates.
- **Task 09:** several worker processes run parses at once against one PostgreSQL database.

## C. Rules for this task

1. **Start clean:** `git status` is clean, on an up-to-date `main`, and `make test` passes. Branch:
   `task/10-header-dictionary`.
2. **Read before you write:** `billparse/mapper.py` in full, `billparse/layout.py` (`norm_h`), `billparse/attributes.py`,
   `svcres/regions.py`, `svcres/resolver.py` (`add_synonym`, `flush`), `billparse/review.py` (`approve`),
   `billparse/learn.py` (`promote_candidates`, `metrics`), `prices/store.py` (`exists`), `billparse/llm.py`
   (`generate_spec`, `SPEC_GUIDE`), `billparse/pipeline.py` (`_mapper`, `_llm_spec`), and the `/aliases` endpoints in
   `billparse/webapi.py` and the Mapping page in `web/src`. If the code differs from part B, stop and tell the user.
3. **Built-in and learned routes do not change:** every sample read by `builtin` or `learned` gives byte-for-byte
   the same result. Mapper samples may only improve (more fields read, higher confidence), never lose a field or a
   status. Show a before and after table in the report.
4. **No network in tests.** No customer files in the repository.
5. **Migrations:** the new table goes through Alembic (PostgreSQL) and the SQLModel class (SQLite).
6. **Docs:** README "Routing" and "Learning loop", and `example.env` for any new setting.
7. **Finish with a report:** what changed, each "Done when" item and how you verified it, the seed entries you are
   unsure of (for the user to check), and anything you decided that this file does not specify.

## D. Words

- **Header:** a column's name as printed. **Normalized header:** `norm_h(CAMEL.sub(" ", header))`, as
  `header_score` already does.
- **Settled column:** a column the mapper mapped with a score of at least 3.0.
- **Detector:** a test on a column's values that can propose a field with no help from the header.

---

## E. The task

### In one sentence

Let the free mapper read more customer layouts, including non-English ones and every cost and ID column the system
now has, so fewer files need the LLM. It does this with header rules for every field, a seed dictionary of foreign
headers, headers learned from approved results, and columns recognized by their values.

### 1. Header rules for the missing fields (`mapper.HEADER_RULES`)

Add rules for these fields. The weights follow the existing ones: specific phrases 3.0–3.5, loose words 2.0.

| Field | Headers (regex words) |
|---|---|
| `list_cost` | list cost, on demand cost, public cost, cost at list, retail cost |
| `effective_cost` | effective cost, amortized cost, amortised cost, net amortized |
| `contracted_cost` | contracted cost, negotiated cost |
| `list_unit_price` | list price, list unit price, public price, on demand rate, on demand price, retail price, payg price |
| `contracted_unit_price` | contracted price, contracted unit price, negotiated price, ea price |
| `credits` | credit, credits, promotion, promo |
| `resource_id` | resource id, resource, instance id, arn, resource uri |
| `sku_id` | sku id, product sku, part number (move `sku id` and `part number` here from `product_code`) |
| `sku_price_id` | sku price id, rate code, price id, offer term rate code |
| `meter_id` | meter id, meter guid |
| `operation` | operation |
| `tenancy` | tenancy |
| `license_model` | license model, licence model, license type, ahb, azure hybrid benefit |
| `pre_installed_sw` | pre installed sw, pre installed software, software |
| `purchase_option` | purchase option, payment option, upfront |
| `pricing_term` | pricing term, term type |
| `commitment_type` | commitment type, reservation type, benefit type |
| `commitment_term` | commitment term, term length, lease length, term |
| `commitment_status` | commitment status, utilization status |
| `provider` | provider, cloud, cloud provider, vendor |
| `line_type_raw` | line item type, record type, charge category |

- Also add `NEGATIVE` entries so that, for example, `list cost` never scores for `billed_cost`, `unit price` never
  for `list_unit_price`, and an `id` header never for a cost field.
- **Leave `coverage` out.** It is derived by `coverage.apply`, never read.
- **`pricing_model` vs `commitment_type`:** keep the existing `pricing_model` rule. Make the new rules specific
  enough that one header never scores 3.0 or more for both.

**Two unit-price columns, decided by the data, not the header word.** When a file has two or more columns that score
for `unit_price` or a `*_unit_price` field (for example Azure's `UnitPrice` and `EffectivePrice`), compute line math
on the sample rows for each: `quantity × price ≈ billed_cost` within 1%.

- The column with the best line math becomes `unit_price`.
- Each other column goes to `list_unit_price` if its header says list, public or on demand; otherwise to
  `contracted_unit_price`.
- If none fits line math, keep today's choice and add a mapper warning.

### 2. Seed dictionary (`data/header_synonyms.seed.json`, in git)

- Structure: `{"<canonical field>": {"<language>": ["header", ...]}}`, headers written as printed. They are
  normalized when loaded.
- **Fields:** `billed_cost`, `quantity`, `unit`, `unit_price`, `list_unit_price`, `list_cost`, `effective_cost`,
  `service_raw`, `description`, `region`, `account_id`, `account_name`, `currency`, `charge_start`, `charge_end`,
  `tax`, `invoice_id`, `resource_id`, `instance_type`, `credits`.
- **Languages:** German, French, Spanish, Portuguese, Italian, Dutch, Japanese. Add English variants the rules miss
  (for example `Amount (USD)`, `Ext. Price`, `Cost (Net)`).
- Write the entries carefully. A wrong one maps a column wrong on every file, which is worse than a missing one.
  List the ones you are unsure of in the report.
- Make sure the German sample's headers are covered (open `samples/make_samples.py` to see them) without writing the
  dictionary to fit that one file.

### 3. Learned dictionary (table `header_synonym`, `billparse/headers.py`)

| Column | Type | Notes |
|---|---|---|
| `header_norm` | str | primary key, with `field` |
| `field` | str | a canonical field |
| `count` | int | times confirmed |
| `source` | str | `review`, `spec`, `admin` |
| `rejected` | bool | true: this header must **not** map to this field |
| `created`, `updated` | float | |
| `updated_by` | str, nullable | |

It lives in PostgreSQL, not a JSON file, because several workers (task 09) learn at once.

`HeaderDictionary` (one per process, cached; reloaded when `max(updated)` changes, checked at most every 60 s) has
these methods:

- `lookup(header) -> (field, score) | None`. Use the learned dictionary first, then the seed.
  - A learned entry with `count >= 1` from `review` or `admin`, or `count >= 2` from `spec`, scores **3.5**.
  - A seed entry scores **3.0**.
  - If a header has two learned fields that each pass that bar, it is ambiguous: no hit.
- `learn(header, field, source, by=None)`: insert or count up, in one statement (`INSERT ... ON CONFLICT ... DO
  UPDATE`). Never learn the fields `coverage` or `provider`. Never learn headers of one or two characters, or purely
  numeric headers.
- `reject(header, field, by=None)` and `remove(header, field)`.

**Scoring:** in `map_columns`, a dictionary hit for `(header, field)` adds a candidate pair `(score +
value_score(field, profile), field, header)`, even when no rule matches. A rejected pair is never added, whether
from a rule, the seed or a detector.

### 4. Value detectors (`mapper.py`)

Detectors run on the sample values `_samples_frame` already takes (200 rows). A detector that fits at least 80% of
the non-empty values proposes its field with score **3.0**, even if the header matches nothing. If a header rule and
a detector disagree, the higher total wins; a tie goes to the header.

| Field | Detector |
|---|---|
| `instance_type` | `attributes.instance_type(v)` is not None. When the price catalog is loaded, at least half the distinct values must also exist in it (`PriceCatalog.exists`, one query for the column's distinct values). |
| `region` | the value is a region code or name per `svcres/regions.py`, or an AWS `REGION_PREFIX` code |
| `account_id` | a 12-digit number (AWS), a GUID (Azure subscription), or a GCP project ID (`^[a-z][a-z0-9-]{4,28}[a-z0-9]$`, only with a header that is not a rule match for another field) |
| `currency` | an ISO 4217 three-letter code (keep the set in the module) |
| `usage_type` | `attributes.USAGE_TYPE` matches |
| `operation` | `RunInstances…`, `CreateDBInstance…`, `CreateCacheCluster…` and similar (AWS operation names: start from the values the catalog has) |
| `resource_id` | `arn:aws:`, `/subscriptions/…/resourceGroups/…`, `//compute.googleapis.com/…`, or `i-` + 8 or 17 hex characters |
| `meter_id` | a GUID, when the column is not already `account_id` and the file's provider is Azure |
| `sku_id` | when the price catalog is loaded, at least half the distinct values are catalog SKUs (`exists`) |

- Cache catalog checks per parse.
- With no catalog, the two catalog-backed detectors use their pattern only for `instance_type`. `sku_id` makes no
  proposal without the catalog.
- A detector never proposes a numeric field: costs and quantities need a header or the dictionary.

### 5. Learning hooks

- **Review approved** (`review.approve`): when no charge changed (the same condition that verifies the candidate
  spec), call `learn(header, field, "review", by)` for each `columns[field]["from"][0]` of the spec that read the
  file.
- **Spec promoted** to `verified` (`learn.promote_candidates`, and the agent's proved spec once it is verified):
  `learn(..., "spec")` for its columns.
- **Spec repaired** (a new spec version maps a header to a different field than the previous version):
  `reject(header, old_field)` with source `spec`. That counts as one rejection; an admin can remove it.
- **Endpoints** in `webapi.py`, modeled on `/aliases`: `GET /headers` (filter by field, source, rejected), `POST
  /headers` (`{header, field}`, source `admin`), `POST /headers/reject`, `DELETE /headers/{header_norm}/{field}`.
- **Web app:** a "Column headers" section on the Mapping page that lists, adds, rejects and deletes entries, like
  the aliases section. Regenerate the API types the way the repo does. `make typecheck` passes.

### 6. The LLM only maps what is left (`pipeline._llm_spec`, `llm.generate_spec`)

When the mapper attempt was not accepted but settled some columns (score ≥ 3.0):

1. Pass them to `generate_spec` as fixed: a "Settled columns" block listing `field <- header` per line, and the
   instruction to keep them and map only the other columns. For both of task 08's candidates.
2. After the LLM answers, if a settled mapping was changed, keep the LLM's spec. The checks decide, as for any
   candidate. Record it in `att.verdict["warnings"]` ("the spec changed a settled column: field").

If the mapper settled nothing, today's prompt is used unchanged.

### Tests (`tests/test_headers.py`, plus the existing suites)

- **German:** with the seed loaded and no LLM, `german_invoice.xlsx` parses through the **mapper** with zero LLM
  calls and status `reconciled` or `unreconciled`. Add it to `EXPECTED` with route `mapper` and its total. Keep
  `GERMAN_SPEC`'s LLM test as it is.
- **New fields:** a CSV with headers `Service`, `Usage Quantity`, `Unit Price`, `List Price`, `List Cost`, `Amortized
  Cost`, `Cost`, `Resource ID`, `SKU`, `Region`, `Instance Type`, `Date` maps each one to its own field.
  - With the fixture price catalog, `cost_order` and `printed_list_price` run, not skip.
- **Two unit prices:** `UnitPrice` and `EffectivePrice`, where only `EffectivePrice` × quantity = cost: it becomes
  `unit_price` and `UnitPrice` becomes `contracted_unit_price`. Then the reverse.
- **No headers:** a CSV with headers `Col_1` … `Col_9` holding an AWS account ID, a region, an instance type, a
  usage type, a resource ARN, a date, a currency, a quantity and a cost maps the first seven by their values.
  Quantity and cost do not map by value: that is by design, and the test shows it.
- **Catalog-backed:** with the fixture catalog, a column of real SKUs maps to `sku_id`. A column of random
  12-character codes does not. A column of instance-type-shaped values that the catalog does not have is not
  `instance_type`.
- **Learning:** approving a review with no changes learns its headers. A second file with the same unusual headers
  then maps through the dictionary. A spec source needs two confirmations. Two learned fields for one header means
  no hit.
- **Rejecting:** `reject` stops a header from mapping to that field, from rules, seed and detectors alike. A spec
  repair that remaps a header records the rejection.
- **Concurrency:** two threads learning the same header 50 times each end with `count == 100` (one statement per
  learn, no read-then-write).
- **LLM prompt:** with `FakeLLM`, the prompt lists the settled columns, and the fallback for no settled columns is
  unchanged.
- **Regression:** every `builtin` and `learned` sample is identical (rule 3). Mapper samples do not lose fields.

### Done when

- [ ] Header rules for every readable canonical field; two unit prices decided by line math.
- [ ] Seed and learned dictionaries, scoring, rejections.
- [ ] Value detectors, catalog-backed where the catalog is loaded.
- [ ] Learning hooks, endpoints and the Mapping page section.
- [ ] The LLM maps only unsettled columns.
- [ ] The German sample needs no LLM; the before and after table of the mapper samples is in the report.
- [ ] `no_llm_share` (`learn.metrics`) does not fall on the user's local corpus. Ask the user to run
      `make corpus-check` (customer files stay on their machine).
- [ ] README "Routing" and "Learning loop" updated.

### Do not

- Do not let a detector propose a cost or quantity column.
- Do not learn from unapproved or unverified results.
- Do not change built-in specs, the checks, pricing, or the contract.
- Do not store the learned dictionary in a file.
