# High-threshold walk-forward

所有历史测试 fold 均标为 `HISTORICAL_ROLLING_PSEUDO_OOS`；只有在参数已锁定后、且测试起点不早于显式 `future_oos_start` 的数据才标为 `FUTURE_TRUE_OOS`。

主结果将100/150/200 bps作为三个独立固定场景，不进行跨阈值训练选择。Gate pair使用Gate因果标签，非Gate pair明确标为NO_GATE_REGIME_FILTER。测试窗使用训练末尾lookback作为warm-up，但只允许测试期信号。信号由 bar close 确认，仅使用下一根连续 bar open 作为成交代理。每个 pair 同时最多一个仓位。`same_pair_blocked_overlap_signal_count` 是持仓中被拦截的同 pair 信号；`cross_pair_overlapping_event_count` 是与其他 pair 仓位时间相交的已实现事件数。资金占用率以每个 pair 一单位容量计算。未退出及无下一根bar的事件不会被删除；主置信区间为day-block bootstrap，同时输出naive event bootstrap。历史 K 线不是 BBO。
