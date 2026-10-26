# Task 03: Attribute, ID and commitment columns from export files

**Order:** after 02. **Needs:** 01, 02 (the large path and unique-value text extraction). **STOP gate:** none, but **stop at the end for user review** (they
will run the corpus check on real exports).
**Read first:** `00-START-HERE.md`, then task 01 (you build on its fields).

## In one sentence

When a provider export has a column for a line's instance type, operating system, usage type, IDs or commitment,
read it from that column instead of guessing from description text, at any file size.

## Where the values come from

**From columns printed in the customer's file only.** Text extraction (`attributes.py`) stays as the fallback for
invoices and custom files. No prices, no catalog lookups. Task 06 uses these fields to look up prices later, which
is why they must be right.

## What exists today (verified on the samples)

| Sample | Lines | instance_type | operating_system | pricing_model | commitment_id | meter_id |
|---|---|---|---|---|---|---|
| `aws_cur2.csv.gz` | 1,648 | 650 (regex on description) | 0 | 0 | 0 | n/a |
| `azure_ea_usage.csv` | 1,109 | 0 | 0 | 0 | 0 | 0 (it is in `attributes`) |
| `focus_gcp.parquet` | 120 | 0 | 0 | 0 | 0 | n/a |

Why:

- `specs/builtin/aws-cur2.json` and `aws-cur-legacy.json` map no product attribute columns, no operation, no
  reservation or Savings Plan ARN. `line_item_usage_type` is used only as a fallback for `description`.
- Before task 02, `pipeline.ATTRIBUTE_ROWS = 100_000` skipped text extraction above 100k rows. Task 02 removed the
  cutoff and made extraction run on unique values, but text is still a guess: export columns must be read.
- `specs/builtin/azure-usage-details.json` does not map `MeterId`, `ReservationId`, `BenefitId`, `ResourceId`,
  `AdditionalInfo`. Azure meter names like `D4s v5` do not match `attributes.AZURE_SIZE`, which needs the
  `Standard_` prefix.
- `specs/builtin/focus.json` does not map `SkuId`, `SkuPriceId`, `ResourceId`, `CommitmentDiscountId`,
  `ListUnitPrice`, `ContractedUnitPrice`.
- `specs/builtin/gcp-billing-export.json` does not map `sku.id`.
- `billparse/schema.py` `Charge` already has `operation`, `sku_id`, `sku_price_id`, `meter_id`, `commitment_id`,
  `commitment_type`, `usage_type`, `instance_type`, `operating_system`, `database_engine`, `deployment_option`. It
  has no `tenancy`, `license_model`, `resource_id`, `list_unit_price`, `contracted_unit_price`.
- `contract.TEXT_FIELDS` lists the text fields `contract._charge` copies to typed fields. `contract._charge`
  currently fills `sku_id` from `product_code`.

## Steps

1. **Canonical fields** (`transforms.py` `CANONICAL`, with one-line descriptions):
   - text fields: `operation`, `tenancy`, `license_model`, `sku_id`, `sku_price_id`, `meter_id`, `resource_id`,
     `pricing_term`, `purchase_option`;
   - unit prices: `list_unit_price`, `contracted_unit_price`. Add both to `NUMERIC` and to `spec.NEVER_ADDED`.

   `instance_type`, `operating_system`, `database_engine`, `deployment_option`, `usage_type`, `pricing_model` and
   `commitment_id` already exist.

2. **Schema** (`schema.py`), `SCHEMA_VERSION = "1.5"`:
   - `Charge`: add `tenancy`, `license_model`, `resource_id`, `pricing_term`, `purchase_option`
     (`CitedValue[str]`), and `list_unit_price`, `contracted_unit_price` (`CitedValue[Decimal]`).
   - `contract.TEXT_FIELDS`: add the new text fields and `operation`, `sku_price_id`, `meter_id`.
   - `contract._charge`: fill `sku_id` from the new `sku_id` field when present, else from `product_code` as today.
     Copy the two unit prices like `unit_price`.
   - **Do not** add `resource_id` to `ROLLUP_KEYS`: it would stop roll-ups from merging anything.
   - **Public contract** (`export.py`): add the same seven fields to `export.Charge` and to the field list in
     `export.to_schema`, or consumers never receive them. Comment each one with its FOCUS name where one exists:
     `list_unit_price` ≈ `ListUnitPrice`, `contracted_unit_price` ≈ `ContractedUnitPrice`, `resource_id` ≈
     `ResourceId`. Test that `POST /parse` returns them for a new sample.

3. **A transform to read map or JSON cells** (`transforms.py`, registered in `TRANSFORMS`): `json_get` takes a key
   and returns that key's value from a JSON object cell (a string or a dict). Example: `{"json_get": "ServiceType"}`.
   CUR 2.0 stores many product attributes in a `product` map column; Azure stores the VM size in `AdditionalInfo`.
   Document it in `specs/README.md`.

4. **Built-in specs.** Add the mappings below. Use `coalesce` where a field can come from several columns. Verify
   every name (START-HERE rule 10).
   - **AWS CUR legacy:**
     - `usage_type` ← `lineItem/UsageType`
     - `operation` ← `lineItem/Operation`
     - `instance_type` ← `product/instanceType`
     - `operating_system` ← `product/operatingSystem`
     - `tenancy` ← `product/tenancy`
     - `license_model` ← `product/licenseModel`
     - `database_engine` ← `product/databaseEngine`
     - `deployment_option` ← `product/deploymentOption`
     - `sku_id` ← `product/sku`
     - `list_unit_price` ← `pricing/publicOnDemandRate`
     - `pricing_term` ← `pricing/term`
     - `purchase_option` ← `pricing/PurchaseOption`
     - `commitment_id` ← `reservation/ReservationARN`, else `savingsPlan/SavingsPlanARN`
     - `resource_id` ← `lineItem/ResourceId`
   - **AWS CUR 2.0:** the snake_case equivalents of the legacy columns. Product attributes may be flat columns
     (`product_instance_type`) or keys of the `product` map column; support both, using `coalesce` with `json_get`
     on `product`.
   - **Azure cost details:**
     - `meter_id` ← `MeterId`
     - `commitment_id` ← `ReservationId`, else `BenefitId`
     - `resource_id` ← `ResourceId`, else `InstanceName`
     - `list_unit_price` ← `PayGPrice`
     - `contracted_unit_price` ← `UnitPrice`
     - `instance_type` ← `AdditionalInfo` with `{"json_get": "ServiceType"}`
   - **FOCUS:**
     - `sku_id` ← `SkuId`
     - `sku_price_id` ← `SkuPriceId`
     - `resource_id` ← `ResourceId`
     - `commitment_id` ← `CommitmentDiscountId`
     - `list_unit_price` ← `ListUnitPrice`
     - `contracted_unit_price` ← `ContractedUnitPrice`
     - Columns starting with `x_` only when a documented provider equivalent exists.
   - **GCP billing export:**
     - `sku_id` ← `sku.id` / `sku_id`
     - `resource_id` ← `resource.name`, when present
     - `instance_type` ← system labels (`compute.googleapis.com/machine_spec`), when present, via `json_get`

5. **Azure VM size from meter names** (`attributes.py`): when `instance_type` is empty, the line's service is
   Virtual Machines, and the meter name or sub-category looks like a size (`D4s v5`, `E8ds v5`, `B2ms`), produce
   `Standard_D4s_v5` (`Standard_` + the name with spaces replaced by `_`). Keep the original in
   `attributes["meter_name"]`. Spot and low-priority suffixes (`D4s v5 Spot`, `D4s v5 Low Priority`) are removed
   from the size and recorded in `pricing_model`.

6. **Large files.** Task 02 already removed the 100k-row cutoff and made `attributes.extract` work on unique
   values. Check that every new mapping and the Azure size normalizer also run in `bigfile.py`'s per-chunk steps,
   and that the normalizer works on unique meter names, not per row.

## New samples and tests

Do not change existing samples. Add these generators:

1. `aws_cur_legacy_full()`: a legacy CUR with all the columns from step 4 filled.
2. `aws_cur2_flat()` and `aws_cur2_map()`: CUR 2.0 with flat product columns, and with a `product` map column.
3. `azure_usage_full()`: an Azure file with `MeterId`, `ReservationId`, `PayGPrice`, `UnitPrice`, `ResourceId` and
   `AdditionalInfo`, plus meter names like `D4s v5`.
4. `focus_full()`: a FOCUS file with the ID and unit-price columns.
5. `gcp_export_full()`: a GCP export with `sku.id` and system labels.

New file `tests/test_attribute_columns.py`:

- For each new sample, every compute or database usage line has `instance_type`. AWS lines also have
  `operating_system`, `usage_type` and `operation`. Azure lines have a typed `meter_id`. Lines with a commitment
  have `commitment_id`.
- Azure `D4s v5` becomes `Standard_D4s_v5`.
- On the large path (set `LARGE_FILE_MB` very low), a new CUR sample gets the same attribute fields as on the small
  path.
- `SCHEMA_VERSION == "1.5"`.

## Done when

- [ ] Tests, lint and typecheck pass; old `EXPECTED` entries unchanged.
- [ ] The table at the top, re-run on the new samples, shows 100% for every field the file has a column for.
- [ ] The large path fills the same fields as the small path.
- [ ] `llm.SPEC_GUIDE` lists the new canonical fields automatically (it prints `CANONICAL`; check the prompt still
      fits the model's limits).
- [ ] `specs/README.md` documents `json_get`; README updated.
- [ ] **Stop and report to the user.** Ask them to run `make corpus-check` on real exports before task 04 starts.

## Do not

- Do not look up prices or validate against a catalog (tasks 05–07).
- Do not migrate learned specs that already exist in users' stores.
