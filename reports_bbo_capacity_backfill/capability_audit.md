# Legacy BBO capacity backfill capability audit

This audit is evidence-only. It does not mutate BBO parts and never applies current
metadata backward without a time match.

- Rows inspected: 13718080
- Backfillable legacy rows: 163904
- Non-backfillable legacy rows: 13554176
- Safe legacy backfill ratio: 1.1948%
- Maximum accepted metadata age: 6 hours
- Reasons: `{'NO_CONTEMPORANEOUS_METADATA': 13418183, 'MULTIPLIER_STABILITY_UNPROVEN': 135993, 'SIZE_UNIT_UNKNOWN': 0}`

A legacy row is backfillable only when a valid prior metadata snapshot is within the
age bound and a following snapshot proves the same contract multiplier. All other
rows remain `CAPACITY_UNKNOWN`; price fields are never used to infer a multiplier.

See `date_exchange_coverage.csv` for UTC date/exchange coverage.
