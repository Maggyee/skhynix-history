# Gate causal-regime audit

The audit found and fixed nested rolling MAD/sign windows and incomplete post-gap re-warm. Threshold parameters were not tuned. Synthetic tests cover persistent same-sign basis, one transient spike, a gap with full reaccumulation, and normal low spread.

| causal_regime | bar_count | after_bars |
|---|---|---|
| NORMAL | 4134 | 100 |
| STALE_OR_INVALID | 84 | 4013 |
| STRUCTURAL_PREMIUM | 0 | 100 |
| TRANSIENT_DISLOCATION | 0 | 5 |

The study was regenerated after the fix. Before/after bar-count changes are implementation effects, not evidence of profitability.
