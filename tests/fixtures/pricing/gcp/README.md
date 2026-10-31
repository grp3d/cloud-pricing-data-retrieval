# GCP Cloud Billing Catalog fixtures

Trimmed from the full catalog download of 2026-10-30 (`/v1/services`, `/v2beta/skus`,
`/v2beta/skus/-/prices?currencyCode=USD`). Responses never contain the API key, which
was sent only in the `X-goog-api-key` header.

Each listing keeps its real shape and is split into pages with fixture tokens:
`nextPageToken` is `fixture-<listing>-p0002` on the first page and `""` on the last
(the API's own end marker).

| SKU | Why it's here |
|---|---|
| `BB77-5FDA-69D9` N2 Instance Core running in Americas | `TYPE_MULTI_REGIONAL` (`us-central1`, `us-east1`, `us-west1`); prices `Default`, `Compute Flexible CUD - 1 Year`, `- 3 Year` |
| `5B01-D157-A097` N2 Instance Ram running in Americas | same |
| `0002-C3CC-1713` | `TYPE_REGIONAL` in `us-east1` |
| `0009-6F35-3126` | `TYPE_REGIONAL` in `europe-west1` |
| `2D3F-8043-D389` | `TYPE_REGIONAL` with region `US` (upper case) |
| `0003-CFD5-F4DB` | `TYPE_GLOBAL` |
| `00CC-93E2-6DC7` | no `geoTaxonomy` |
| `53CC-D5FE-D895` | no `productTaxonomy.taxonomyCategories` |
| `0164-F76D-680A` | a `businessEntities/Maps` service (out of scope by default) |
| `0002-17F9-0731` | a price with 3 tiers |
| `00DF-CF91-B364` | `valueType: unspecified` (no `rate`) |
| `001B-3860-A0CF` | `unitQuantity` `1000000` |
| `006A-EFE2-3E22` | **hand edit**: its price entry was removed (a SKU without price) |
| price of `0001-0118-796B` | **hand edit**: its SKU was removed (a price without SKU) |

**Other hand edits**: the first tier of `001B-3860-A0CF`'s first price has
`listPrice.nanos: "12.5x"` (malformed).

`services-p0001.json` lists every service the fixture SKUs reference (one page).
