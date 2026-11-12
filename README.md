# quant-backtest-engine

回测库（`backtest.duckdb`）的**建表方**与净值/绩效计算组件。

- 项目/文件夹：`quant-backtest-engine`
- Python 包名：`backtest_engine`
- 安装后的 distribution：`quant-backtest-engine`

## 1. 定位

```text
market.duckdb      ← quant-data-pipeline 写基础行情 / factor_research 写因子
backtest.duckdb    ← 本项目建表；quant-baseline 写基准；本项目写净值
```

职责边界（硬性）：

- **只由本项目建 `backtest.duckdb` 的表**：`scripts/init_backtest_db.py`，DDL 来自 `quant_schema.backtest_ddl`（`quant-db-schema` 项目）。
- **`quant-baseline` 不建表**，只 upsert `bt_benchmarks / bt_baselines / bt_baseline_weights / bt_compute_log`。
- **本项目不建 `market.duckdb` 的表**，读取时使用 `read_only=True`，或 `ATTACH ... (READ_ONLY)` 跨库计算。
- 超额收益在 SQL 视图 `v_bt_nav_excess` 里算，不把基准序列搬到 Python。

## 2. 目录

```text
quant-backtest-engine/
├── configs/database.yaml          # market / backtest 路径与资源
├── configs/execution.yaml         # 手续费 / 滑点 / 成交约束
├── docs/README.md                 # 按需阅读索引
├── docs/db_schema.md              # 表结构文档
├── scripts/
│   ├── init_backtest_db.py        # init / check，单事务 + 校验 + 版本行
│   ├── run_nav.py                 # 收益 CSV -> bt_runs / bt_nav / bt_run_metrics
│   └── demo_run.py                # 端到端 smoke test（只读 market，写 backtest）
├── src/backtest_engine/           # Python 包名保持 backtest_engine
│   ├── core/config.py             # 配置加载
│   ├── core/execution.py          # ExecutionConfig / 固定滑点与成交约束
│   ├── core/db.py                 # market 只读 / backtest 读写 / ATTACH
│   └── engine/
│       ├── nav.py                 # 登记运行、写净值、算指标
│       └── metrics.py             # 年化、夏普、回撤、超额、IR
└── tests/
```

## 3. 快速开始

```bash
# 0) 环境变量
cp .env.example .env
# 在 .env 填写 MARKET_DB_PATH / BACKTEST_DB_PATH（.env 已被 gitignore）

# 依赖（示例；按自己的项目布局调整相对路径）
uv add --editable ../quant-db-schema
uv add --editable ../quant-data-pipeline
uv add --editable ../quant-baseline
uv add duckdb polars pyarrow pytz pyyaml numpy scipy
uv sync

# 1) 建表（幂等；会写入 component='backtest' 的版本行）
uv run python scripts/init_backtest_db.py init

# 2) 校验
uv run python scripts/init_backtest_db.py check

# 3) 基准先由 quant-baseline 写入，然后跑 demo 净值
uv run python scripts/demo_run.py --run-id demo_eqw --benchmark-id BI_EQW_CORE

# 4) 任意策略日收益 CSV 入库
uv run python scripts/run_nav.py --run-id my_run --strategy-id mom `
    --returns outputs/mom_returns.csv --benchmark-id BI_HS300 `
    --execution-config configs/execution.yaml
```

`--execution-config` 会把配置校验后的摘要写入 `bt_runs.cost_model`；
`run_nav.py` 仍是“收益 -> 净值”模式，不会凭空生成成交价或成交记录。

### 测试库

```bash
# 初始化并复制正式基准
uv run python scripts/init_test_db.py
uv run python scripts/sync_test_backtest.py --apply

# 用 test profile 生成测试 run
uv run python scripts/demo_run.py --profile test --run-id demo_test --benchmark-id BI_EQW_CORE

# 验证通过后发布（默认 dry-run）
uv run python scripts/promote_test_run.py --run-id demo_test
uv run python scripts/promote_test_run.py --run-id demo_test --apply
```

测试库路径来自 `BACKTEST_TEST_DB_PATH`；正式库与测试库路径不能相同。

`run_nav.py` 的 CSV 至少包含：

```csv
trade_date,ret_1d
2024-01-02,0.0
2024-01-03,0.0123
```

本项目采用 `src/` 布局，`uv sync` 会以 editable 方式安装 `backtest_engine`；
测试通过 `pyproject.toml` 的 `pythonpath = ["src", "scripts"]` 解析包和脚本，
不使用 `sys.path.insert`。

## 4. 数据表

| 表 | 写入方 | 主键 | 用途 |
| --- | --- | --- | --- |
| `meta_schema_version` | 本项目 init | `component` | 独立的 `backtest` 版本行 |
| `bt_runs` | 本项目 | `run_id` | 策略配置、状态、参数快照 |
| `bt_nav` | 本项目 | `(run_id, trade_date)` | 策略日频净值/回撤 |
| `bt_run_metrics` | 本项目 | `(run_id, metric_name)` | 绩效指标 |
| `bt_benchmarks` | quant-baseline | `benchmark_id` | 基准定义物化副本 |
| `bt_baselines` | quant-baseline | `(benchmark_id, trade_date)` | 基准点位/净值 |
| `bt_baseline_weights` | quant-baseline | `(benchmark_id, trade_date, symbol)` | 合成基准权重 |
| `bt_compute_log` | quant-baseline | `run_id` | 基准计算批次日志 |

字段、视图与写入约定见 [docs/db_schema.md](docs/db_schema.md)；按需阅读索引见
[docs/README.md](docs/README.md)。

## 5. 并发与跨库

DuckDB 是单写多读：

- `market.duckdb`：quant-data-pipeline / factor_research 持 RW 写；本项目**只读**打开，可以并发。
- `backtest.duckdb`：同一时刻只允许一个 RW 连接；`init` 与写净值要串行。
- 跨库计算示例（backtest 连接内）：

```sql
ATTACH '${MARKET_DB_PATH}' AS market (READ_ONLY);

SELECT n.run_id, n.trade_date, n.nav, b.nav AS bench_nav
FROM bt_nav n
JOIN bt_baselines b
  ON b.benchmark_id = n.benchmark_id AND b.trade_date = n.trade_date
WHERE n.run_id = 'demo_eqw';
```

## 6. 执行成本与成交约束

[configs/execution.yaml](configs/execution.yaml) 当前提供固定值：

```yaml
fees:
  commission_bps: 2.0
  min_commission: 5.0
  stamp_tax_bps: 10.0       # 仅卖出
  transfer_fee_bps: 0.0
slippage:
  mode: fixed
  bps: 5.0
constraints:
  mode: fixed
  allow_limit_up_buy: false
  allow_limit_down_sell: false
  allow_suspension_trade: false
  allow_st_trade: true
  max_participation_rate: 0.1
```

`ExecutionModel` 负责：

- 整手：`quantity // lot_size * lot_size`；
- 滑点：买入上浮 `bps`，卖出下调 `bps`；
- 费用：佣金 `max(notional * commission_bps/10000, min_commission)`，
  卖出加印花税，过户费可选；
- 约束：涨停不可买、跌停不可卖、停牌不可成交、ST 开关、最大参与率。

### 动态扩展

`slippage.mode` / `constraints.mode` 走注册表：

```python
from backtest_engine.core.execution import (
    SlippageConfig,
    register_slippage_policy,
)

@register_slippage_policy("volume_impact")
def build_slippage(config: SlippageConfig):
    class Policy:
        def bps(self, order):
            # 这里可以读盘口/成交量/波动率，返回动态 bps
            return 5.0
    return Policy()
```

然后在 YAML 中：

```yaml
slippage:
  mode: volume_impact
  bps: 5.0
  params: {}
```

未来只需新增注册函数 + 改 YAML，不需要改 `ExecutionModel`。

> 注意：当前 `run_nav.py` 仍是收益入库器，`ExecutionModel` 尚未接入
> 订单/持仓级撮合；真正扣费需要下一步接 `bt_trades` 与持仓模拟。

## 7. 测试

```bash
uv run --with pytest pytest tests -q
```

测试全部使用 `tmp_path` 下的临时库，不触碰 `<data-root>`。
