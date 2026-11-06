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
├── docs/db_schema.md              # 表结构权威文档
├── scripts/
│   ├── init_backtest_db.py        # init / check，单事务 + 校验 + 版本行
│   ├── run_nav.py                 # 收益 CSV -> bt_runs / bt_nav / bt_run_metrics
│   └── demo_run.py                # 端到端 smoke test（只读 market，写 backtest）
├── src/backtest_engine/           # Python 包名保持 backtest_engine
│   ├── core/config.py             # 配置加载
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
uv run python scripts/run_nav.py --run-id my_run --strategy-id mom \
    --returns outputs/mom_returns.csv --benchmark-id BI_HS300
```

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

字段、视图与写入约定见 [docs/db_schema.md](docs/db_schema.md)。

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

## 6. 测试

```bash
uv run --with pytest pytest tests -q
```

测试全部使用 `tmp_path` 下的临时库，不触碰 `<data-root>`。
