# Azure Retail Prices fixtures

Trimmed from live responses of `https://prices.azure.com/api/retail/prices`
(api-version `2023-01-01-preview`, no credential) on 2026-10-30. The page shape is the
real one (`BillingCurrency`, `CustomerEntityId`, `CustomerEntityType`, `Items`,
`NextPageLink`, `Count`).

| File | Content |
|---|---|
| `eastus-p0001.json` | 6 `Standard_D4s_v5` items (Consumption Linux with a 2-entry `savingsPlan`, Windows, Low Priority, DevTestConsumption, Reservation 1 Year / 3 Years), 3 tiers of `Hot RA-GZRS Data Stored`, 1 VM item with `effectiveEndDate`. `NextPageLink` is the real `$skip=1000` link. |
| `eastus-p0002.json` | 5 more `Standard_D4s_v5` items (Spot, Low Priority), then the hand edits below. `NextPageLink: null`. |
| `global-empty-p0001.json` | 4 items from `armRegionName eq ''` |
| `global-global-p0001.json` | 4 items from `armRegionName eq 'Global'`, one with `effectiveEndDate` |
| `north-america-p0001.json` | `Bandwidth` items with `armRegionName: "North America"` |

**Hand edits** (not as published), all in `eastus-p0002.json`:

- the first item of `eastus-p0001.json` repeated byte for byte (a duplicate across pages)
- a copy of that item with a new `meterId`, `meterName` "D4s v5 (extra field)" and an unknown field `newSourceField` (for `extra_fields`)
- a copy of the Low Priority item with a new `meterId`, `meterName` "D4s v5 Low Priority (bad price)" and `unitPrice: "n/a"` (non-numeric)

Rebuild with the scratchpad script `build_azure_fixtures.py` from feature 004's planning session.
