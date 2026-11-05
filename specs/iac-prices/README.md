# Price rules for infrastructure as code

`aws.json` tells `billparse/iac.py` (`price`) how to price the rows of a Terraform or CloudFormation inventory with
the AWS on-demand catalog. To price a new resource type, add a rule here; no code changes.

For each row, the rules are tried in order. The first rule whose `match` fits, and whose `requires` and `vars` can
be filled, prices the row. A row that no rule prices gets `price_match_level` = `not_priced` and a reason in
`price_missing`.

| Key | Meaning |
|---|---|
| `name` | A name for people. |
| `match` | `{field: [values]}` (case is ignored) or `{field: {"regex": r}}`. All must fit. |
| `requires` | Fields that must have a value (a number must be above 0). Every rule also needs a quantity above 0. |
| `defaults` | `{field: {"value": v, "note": text}}`: the value to use when the files do not give one. The note goes into `price_assumptions` (default note: `field=v (the default, the files do not say)`). |
| `vars` | `{name: {"from": field, ...}}`: a value for the templates. `map` (with `else`: the value, or a template, for a key not in the map; without `else` the rule does not price the row), `format` (`"{value}-"`, with `else` when the field is empty), `sub` (`[regex, replacement]`), `upper`. |
| `set` | `{field: template or null}`: the catalog keys of the row (`service_code`, `usage_type`, `operation`, ...). |
| `quantity` | An expression (as a spec's `derive`) in the catalog's unit: `quantity * hours`, `quantity * storage_gb`. |
| `unit` | The catalog's unit (`Hrs`, `GB-Mo`). Without it, the row keeps its unit. |
| `note` | A template for `price_assumptions`, after the notes of the defaults. |
| `stop` | When `match` fits but the rule cannot price the row, no later rule tries it. |

Templates use the row's fields (`{instance_type}`, `{storage_gb:g}`), the rule's `vars`, `{hours}` (`hours_a_month`)
and `{prefix}`: the usage type prefix of the row's region with its dash (`USW2-`; `USE1-` for us-east-1; empty for a
region without one).

`maps.engines` changes a Terraform or CloudFormation engine name into the catalog's (`postgres` -> `PostgreSQL`).
`no_rule` gives the reason for a resource type that costs money but has no rule yet.
