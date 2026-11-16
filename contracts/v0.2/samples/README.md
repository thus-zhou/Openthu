# Contract samples

These fixtures are synthetic and contain no real accounts, cookies, credentials, host names or personal records.

| File | Purpose |
|---|---|
| `source-states.json` | Source failure, real empty list and partial read must stay distinguishable. |
| `blocked-source.json` | Unknown source contract is recorded as `BLOCKED`, not as an empty success. |
| `stability.json` | Same-title course, resource and order records keep different stable identifiers. |

`contracts/v0.2/validate.py` validates each evidence object against `source-evidence.schema.json` or `result-envelope.schema.json` and asserts the expected semantics.
