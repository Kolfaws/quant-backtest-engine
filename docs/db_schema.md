# backtest.duckdb — 表结构

> **DDL 唯一来源**：`quant-db-schema/src/quant_schema/backtest_ddl.py`  
> **建表/校验唯一入口**：`scripts/init_backtest_db.py`  
> **版本**：`component='backtest'`，当前 `1.0.0`  
> **写入方**：`quant-baseline` 写基准四表；`quant-backtest-engine` 写运行/净值/指标

本文档描述 `${BACKTEST_DB_PATH}` 的契约。任何字段变更必须先改
`quant-db-schema`、升版本、再改本文档；禁止在业务代码里直接 `ALTER TABLE`。

## 1. 设计约定

- 表前缀 `bt_`：回测评价领域；`meta_`：版本/批次元数据。
- 主键必须显式声明；所有写入走 `INSERT ... ON CONFLICT (...) DO UPDATE`。
- 日期用 `DATE`；时间戳用 `TIMESTAMPTZ`；净值/收益用 `DOUBLE`。
- `bt_nav.nav`、`bt_baselines.nav` 都是**归一净值**（起点为 1.0）。
- 基准与回测的**生命周期分离**：基准可被多个 run 复用，不随 run 删除。

## 2. 表清单

| 表 | 主键 | 写入方 | 说明 |
| --- | --- | --- | --- |
| `meta_schema_version` | `component` | quant-backtest-engine | 独立版本行，`component='backtest'` |
| `bt_runs` | `run_id` | quant-backtest-engine | 策略配置与运行状态 |
| `bt_nav` | `(run_id, trade_date)` | quant-backtest-engine | 策略日频净值 |
| `bt_run_metrics` | `(run_id, metric_name)` | quant-backtest-engine | 绩效指标 |
| `bt_benchmarks` | `benchmark_id` | quant-baseline | 基准定义 |
| `bt_baselines` | `(benchmark_id, trade_date)` | quant-baseline | 基准点位/净值 |
| `bt_baseline_weights` | `(benchmark_id, trade_date, symbol)` | quant-baseline | 合成基准每日权重 |
| `bt_compute_log` | `run_id` | quant-baseline | 基准计算批次日志 |

## 3. 表明细

### 3.1 `meta_schema_version`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `component` | `VARCHAR` | PK | 固定 `backtest` |
| `version` | `VARCHAR` | NOT NULL | `1.0.0` |
| `applied_at` | `TIMESTAMPTZ` | NOT NULL DEFAULT now() | 版本生效时间 |

与 `market.duckdb` 的同名表**互不相干**：两个库各自记录自己的组件版本。

### 3.2 `bt_runs` — 策略回测批次

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | `VARCHAR` | 主键，建议 `<strategy>-<timestamp>-<rand>` |
| `strategy_id` | `VARCHAR` | 策略标识 |
| `strategy_name` | `VARCHAR` | 展示名 |
| `benchmark_id` | `VARCHAR` | 对应 `bt_benchmarks.benchmark_id` |
| `params_json` | `VARCHAR` | 参数快照 JSON |
| `start_date` / `end_date` | `DATE` | 回测区间 |
| `initial_capital` | `DECIMAL(20,4)` | 初始资金，默认 1,000,000 |
| `cost_model` | `VARCHAR` | 成本模型说明 |
| `status` | `VARCHAR` | `pending / running / success / failed` |
| `engine_version` | `VARCHAR` | 引擎版本 |
| `config_hash` | `VARCHAR` | 配置哈希（可空） |
| `created_at` | `TIMESTAMPTZ` | 默认 now() |
| `finished_at` | `TIMESTAMPTZ` | 结束时间 |

### 3.3 `bt_nav` — 策略净值

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | `VARCHAR` | 关联 `bt_runs` |
| `trade_date` | `DATE` | 交易日 |
| `nav` | `DOUBLE` | 归一净值（起点 1.0） |
| `ret_1d` | `DOUBLE` | 当日收益 |
| `cum_ret` | `DOUBLE` | `nav - 1` |
| `drawdown` | `DOUBLE` | 相对历史最高净值的回撤（≤0） |
| `benchmark_id` | `VARCHAR` | 基准代码；用于 `v_bt_nav_excess` |
| `created_at` | `TIMESTAMPTZ` | 默认 now() |

**主键**：`(run_id, trade_date)`。重跑同一 run 会覆盖，不产生重复行。

### 3.4 `bt_run_metrics` — 绩效指标

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | `VARCHAR` | 关联 `bt_runs` |
| `metric_name` | `VARCHAR` | 指标名（见下表） |
| `metric_value` | `DOUBLE` | 指标值（可为 NULL） |
| `computed_at` | `TIMESTAMPTZ` | 计算时间 |

当前写入的指标：

`total_return`、`annualized_return`、`annualized_volatility`、`sharpe_ratio`、
`max_drawdown`、`calmar_ratio`、`win_rate`，以及有基准时的
`benchmark_total_return`、`excess_total_return`、`excess_annualized_return`、
`tracking_error`、`information_ratio`、`beta`。

### 3.5 `bt_benchmarks` — 基准定义

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `benchmark_id` | `VARCHAR` | 主键，如 `BI_EQW_CORE` |
| `name` | `VARCHAR` | 展示名 |
| `kind` | `VARCHAR` | `external` / `synthetic` |
| `category` | `VARCHAR` | `index` / `etf` / `basket` |
| `symbol` | `VARCHAR` | external 的标的代码 |
| `universe_id` | `VARCHAR` | synthetic 的标的池代码 |
| `price_field` | `VARCHAR` | `close` / `open` / `vwap` |
| `adjust` | `VARCHAR` | `pct_chg` / `hfq` / `qfq` / `none` |
| `weight_method` | `VARCHAR` | `equal` / `market_cap` / `fixed` |
| `weight_field` | `VARCHAR` | `total_mv` / `circ_mv` |
| `weight_lag` | `INTEGER` | 市值取 T-N |
| `rebalance_freq` | `VARCHAR` | `daily/weekly/monthly/quarterly/none` |
| `rebalance_align` | `VARCHAR` | `first_trading_day` / `last_trading_day` |
| `base_date` | `DATE` | 基准日 |
| `base_value` | `DOUBLE` | 合成基准点位 |
| `is_active` | `BOOLEAN` | 是否纳入日常同步 |
| `updated_at` | `TIMESTAMPTZ` | 写入时刷新 |

> 配置（`quant-baseline/configs/baseline.yaml`）是唯一事实来源，本表是物化副本。

### 3.6 `bt_baselines` — 基准点位

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `benchmark_id` | `VARCHAR` | 主键之一 |
| `trade_date` | `DATE` | 主键之一 |
| `close` | `DOUBLE` | 点位（外部指数=指数点位；合成=base_value 起算） |
| `ret_1d` | `DOUBLE` | 日收益 |
| `nav` | `DOUBLE` | 归一净值（base_date=1.0） |
| `base_date` | `DATE` | 实际基准日（≥ 配置 base_date 的首个交易日） |
| `base_value` | `DOUBLE` | 基准日点位 |
| `kind` / `category` | `VARCHAR` | 冗余自 `bt_benchmarks` |
| `source` | `VARCHAR` | 数据来源（如 `market.duckdb:fact_index_daily:000300.SH`） |
| `run_id` | `VARCHAR` | 对应 `bt_compute_log.run_id` |
| `created_at` / `updated_at` | `TIMESTAMPTZ` | 首次写入 / 最近覆盖 |

**主键**：`(benchmark_id, trade_date)`。约定来自 quant-baseline 的
`database.yaml: sink.conflict_keys`。

### 3.7 `bt_baseline_weights` — 合成基准权重

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `benchmark_id` | `VARCHAR` | 主键之一 |
| `trade_date` | `DATE` | 主键之一 |
| `symbol` | `VARCHAR` | 主键之一 |
| `weight` | `DOUBLE` | 收盘后实际权重（漂移后，和=1） |
| `target_weight` | `DOUBLE` | 再平衡日目标权重；非再平衡日为 NULL |
| `price` | `DOUBLE` | 当日收盘价（原始） |
| `market_value` | `DOUBLE` | 定权所用市值（T-lag） |
| `is_rebalance_date` | `BOOLEAN` | 当天是否再平衡 |
| `run_id` | `VARCHAR` | 计算批次 |
| `created_at` | `TIMESTAMPTZ` | 默认 now() |

**校验规则**：同一 `(benchmark_id, trade_date)` 的 `SUM(weight)` 必须为 1（容差 1e-9），
否则 quant-baseline 在写库前抛 `ProcessError`。

### 3.8 `bt_compute_log` — 基准计算日志

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | `VARCHAR` | 主键 |
| `job_name` | `VARCHAR` | 固定 `baseline` |
| `target_key` | `VARCHAR` | `benchmark_id` |
| `status` | `VARCHAR` | `success` / `failed` |
| `rows_written` | `BIGINT` | 本次写入行数 |
| `start_date` / `end_date` | `DATE` | 请求区间 |
| `error_message` | `VARCHAR` | 失败摘要（最多 1000 字符） |
| `started_at` / `finished_at` | `TIMESTAMPTZ` | 起止时间 |

## 4. 视图

### 4.1 `v_bt_baselines`

`bt_baselines LEFT JOIN bt_benchmarks`，补全 `name`，用于查询/展示：

```sql
SELECT benchmark_id, name, kind, category, trade_date, close, ret_1d, nav
FROM v_bt_baselines
WHERE benchmark_id = 'BI_HS300';
```

### 4.2 `v_bt_nav_excess` — 超额收益（SQL 层）

按 run 的第一天把基准净值再次归一，然后计算：

- `benchmark_nav_rel = benchmark_nav / 首日 benchmark_nav`
- `excess_nav = nav / benchmark_nav_rel`
- `excess_ret_1d = ret_1d - benchmark_ret_1d`

```sql
SELECT trade_date, nav, benchmark_nav, excess_nav, excess_ret_1d
FROM v_bt_nav_excess
WHERE run_id = 'demo_eqw_core'
ORDER BY trade_date;
```

这样即使回测起点与基准 `base_date` 不同，超额也不会错位。

## 5. 写入与幂等

| 场景 | 写入方式 |
| --- | --- |
| 基准点位重复计算 | `ON CONFLICT (benchmark_id, trade_date) DO UPDATE`，`created_at` 保留首次 |
| 权重重复计算 | `ON CONFLICT (benchmark_id, trade_date, symbol) DO UPDATE` |
| 净值重跑 | `ON CONFLICT (run_id, trade_date) DO UPDATE` |
| 指标重算 | `ON CONFLICT (run_id, metric_name) DO UPDATE` |
| 批次日志 | `ON CONFLICT (run_id) DO UPDATE` |

批大小由 quant-baseline 的 `sink.batch_size` 控制；每批一个事务，失败回滚。
写后可选做全表主键唯一性检查（`verify_after_write`）。

## 6. 版本与迁移

| 版本 | 变更 |
| --- | --- |
| `1.0.0` | 初始结构：8 张表 + 2 个视图 |

迁移流程：

1. 修改 `quant-db-schema/src/quant_schema/backtest_ddl.py`，并在 `constants.py` 升 `BACKTEST_SCHEMA_VERSION`；
2. 在空库执行 `init` -> `check`，确认 `verify_schema` 通过；
3. 对旧库写迁移 SQL（本项目目前未引入迁移框架，结构变更需重建或手工迁移）；
4. 同步更新本文档与 quant-baseline 的 `docs/db_schema.md`。
