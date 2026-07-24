# High-threshold walk-forward

所有历史测试 fold 均标为 `HISTORICAL_ROLLING_PSEUDO_OOS`；只有在参数已锁定后、且测试起点不早于显式 `future_oos_start` 的数据才标为 `FUTURE_TRUE_OOS`。

参数只读取训练窗，测试窗冻结；同一测试窗禁止按测试结果重选。信号由 bar close 确认，仅使用下一根连续 bar open 作为成交代理。每个 pair 同时最多一个仓位。`same_pair_blocked_overlap_signal_count` 是持仓中被拦截的同 pair 信号；`cross_pair_overlapping_event_count` 是与其他 pair 仓位时间相交的已实现事件数。资金占用率以每个 pair 一单位容量计算。历史 K 线不是 BBO。
