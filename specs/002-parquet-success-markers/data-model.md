# Phase 1 Data Model: Parquet Partition Success Markers

This feature adds no new data columns or tables. Its "data model" is a set of files on disk and the rules for when they exist.

## On-disk layout

```text
<parquet_root>/                              # <data_root>/pricing_aws/parquet
├── .locks/                                  # NEW — lock files, never read by consumers
│   ├── <table>/
│   │   └── snapshot_date=<D>.lock           # partition lock (invalidate / finalize)
│   └── _regions/
│       └── snapshot_date=<D>/
│           └── region=<R>.lock              # region-run lock (FR-017)
└── <table>/                                 # service_dim | region_dim | product_dim | product_attribute | price_fact
    └── snapshot_date=<D>/
        ├── _SUCCESS                         # NEW — partition-level "ready" marker (consumer-facing)
        └── region=<R>/
            ├── part-0.parquet               # existing — absent when region had 0 rows for table
            └── _REGION_COMPLETE             # NEW — per-region "done" record (internal)
```

## Entities

### Partition marker — `_SUCCESS`
| Attribute | Value |
|---|---|
| Location | `<table>/snapshot_date=<D>/_SUCCESS` (FR-001, FR-009) |
| Content | empty (FR-008) |
| Audience | downstream consumers |
| Created by | finalize, when the completion rule holds (research.md Decision 5) |
| Removed by | invalidate, when any region starts rewriting `<table>` for `<D>` (FR-006) |

### Region completion record — `_REGION_COMPLETE`
| Attribute | Value |
|---|---|
| Location | `<table>/snapshot_date=<D>/region=<R>/_REGION_COMPLETE` (FR-012) |
| Content | empty |
| Audience | internal: the completion check only |
| Created by | the region-`R` transform, after `<table>` is fully written (or found to be empty, FR-015), and only if every input file parsed (research.md Decision 4) |
| Removed by | invalidate by the region-`R` transform, before it rewrites (FR-013) |

### Partition lock
| Attribute | Value |
|---|---|
| Location | `<parquet_root>/.locks/<table>/snapshot_date=<D>.lock` |
| Content | empty, used only as an `flock` handle |
| Lifetime | created on first use and left in place (it's harmless). The lock itself is released when the holder closes it or the process exits. |

### Region-run lock
| Attribute | Value |
|---|---|
| Location | `<parquet_root>/.locks/_regions/snapshot_date=<D>/region=<R>.lock` |
| Purpose | only one run at a time writes a given region and date (FR-017). Held across the whole per-table loop; always taken before any partition lock. |

### Finalize outcome (in memory, logged)
| Value | Meaning |
|---|---|
| `WRITTEN` | `_SUCCESS` is now present |
| `INCOMPLETE` | ≥1 expected region lacks `_REGION_COMPLETE`. Carries the list of missing regions. |
| `NO_DATA` | all expected regions complete, but none has a data file (FR-007) |
| `SKIPPED` | this region isn't complete for the table (write failed or input parse errors), so finalize wasn't attempted |

## Validation rules

- `_SUCCESS` ⇒ every region in the **current** configured list has `_REGION_COMPLETE`, **and** ≥1 of them has a `*.parquet` file (FR-004, FR-007, FR-014).
- A region folder with data files but no `_REGION_COMPLETE` is treated as not done (FR-014).
- No run creates, modifies or deletes anything under a `snapshot_date=` folder other than the one it's processing (FR-003).
- Region folders for regions not in the configured list are ignored by the completion rule.

## State transitions — one (table, date) partition

```text
            region R starts rewrite (invalidate, under lock)
   ┌──────────────────────────────────────────────────────────────┐
   │                                                              │
   ▼                                                              │
[NOT READY] ──(last expected region writes _REGION_COMPLETE,   [READY: _SUCCESS present]
 no _SUCCESS    finalize under lock, ≥1 region has data)──────────▶
   ▲  │
   │  └─(finalize: regions missing → INCOMPLETE, or all empty → NO_DATA; stays NOT READY)
   └────┘
```

Per region folder, for the same (table, date):

```text
[ABSENT] ─write data (or none if 0 rows)─▶ [DATA, NOT COMPLETE] ─write _REGION_COMPLETE─▶ [COMPLETE]
                                               ▲                                            │
                                               └──────── invalidate (re-run of region) ◀────┘
   (crash or parse error ⇒ stays in DATA, NOT COMPLETE)
```
