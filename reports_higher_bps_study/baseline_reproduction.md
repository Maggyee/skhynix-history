# Baseline reproduction

Reproduced before changing study logic from `main` commit `af361df` on 2026-07-24 UTC.

- `uv run --extra dev pytest -q`: **148 passed**.
- `make analysis-15m`: completed; strict common window ended at 06:15 UTC.
- `make gate-regime-15m`: completed.
- `make high-threshold-walk-forward`: completed (1,710 fold rows; 2,523 event rows).
- The regenerated public-data window extended the committed snapshot from 04:15 to 06:15 UTC. This explains small count/value changes; execution semantics and schemas reproduced.

Selected regenerated aggregate results (all figures are historical 15-minute proxies; costs are research assumptions):

| scope | threshold | cost | signals | realized | mean net | median net | day-block 95% CI |
|---|---:|---:|---:|---:|---:|---:|---:|
| Gate | 100 | 20 | 218 | 167 | 6.76 | -2.00 | [0.53, 15.56] |
| Gate | 100 | 40 | 218 | 167 | -13.24 | -22.00 | [-18.79, -4.18] |
| Gate | 100 | 80 | 218 | 167 | -53.24 | -62.00 | [-59.03, -44.75] |
| Gate | 150 | 20 | 277 | 247 | -2.10 | -6.56 | [-5.42, 1.44] |
| Gate | 150 | 40 | 277 | 247 | -22.10 | -26.56 | [-25.27, -18.45] |
| Gate | 150 | 80 | 277 | 247 | -62.10 | -66.56 | [-65.29, -58.52] |
| Gate | 200 | 20 | 190 | 182 | -4.96 | -9.22 | [-9.00, 0.34] |
| Gate | 200 | 40 | 190 | 182 | -24.96 | -29.22 | [-28.89, -19.61] |
| Gate | 200 | 80 | 190 | 182 | -64.96 | -69.22 | [-68.70, -59.92] |
| non-Gate | 100 | 20 | 118 | 108 | -6.98 | -8.98 | [-10.18, -3.50] |
| non-Gate | 100 | 40 | 118 | 108 | -26.98 | -28.98 | [-30.49, -23.38] |
| non-Gate | 100 | 80 | 118 | 108 | -66.98 | -68.98 | [-70.46, -63.48] |
| non-Gate | 150 | 20 | 25 | 22 | 11.20 | 20.39 | [0.73, 30.69] |
| non-Gate | 150 | 40 | 25 | 22 | -8.80 | 0.39 | [-19.08, 10.69] |
| non-Gate | 150 | 80 | 25 | 22 | -48.80 | -39.61 | [-59.08, -29.31] |
| non-Gate | 200 | 20 | 13 | 12 | 5.04 | -1.05 | [2.00, 8.09] |
| non-Gate | 200 | 40 | 13 | 12 | -14.96 | -21.05 | [-18.00, -11.91] |
| non-Gate | 200 | 80 | 13 | 12 | -54.96 | -61.05 | [-58.00, -51.91] |

At 40 and 80 bps every displayed scope/threshold mean was lower by exactly 20 and 60 bps respectively, confirming one total-cost deduction per realized event. The baseline reproduction therefore passed and extension was allowed to proceed.
