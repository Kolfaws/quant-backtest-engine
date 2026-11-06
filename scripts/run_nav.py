#!/usr/bin/env python3
"""run_nav.py — 把策略日收益 CSV 写成 bt_runs / bt_nav / bt_run_metrics。

这是 backtest_engine 的“最小可用入口”：策略逻辑由 factor_research（或任何
上游）产出日收益，本脚本负责登记运行、落净值、算绩效与超额。

CSV 格式（表头必需）：
    trade_date,ret_1d
    2024-01-02,0.0
    2024-01-03,0.0123

用法：
    python scripts/run_nav.py --run-id demo_eqw --strategy-id eqw_core \
        --returns outputs/demo_returns.csv --benchmark-id BI_HS300
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for candidate in (
    PROJECT_ROOT / "src",
    PROJECT_ROOT.parent / "quant-db-schema" / "src",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from backtest_engine.core.config import load_profile
from backtest_engine.core.db import (
    backtest_connection,
    require_backtest_schema,
)
from backtest_engine.engine.nav import (
    RunSpec,
    compute_metrics,
    mark_run_failed,
    register_run,
    write_nav,
)

LOG = logging.getLogger("run_nav")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_nav.py", description="策略净流入库")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--strategy-name", default=None)
    parser.add_argument("--returns", type=Path, required=True, help="CSV 路径")
    parser.add_argument("--benchmark-id", default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--cost-model", default=None)
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser


def _read_returns(path: Path) -> pl.DataFrame:
    frame = pl.read_csv(path)
    if "date" in frame.columns and "trade_date" not in frame.columns:
        frame = frame.rename({"date": "trade_date"})
    if "trade_date" not in frame.columns or "ret_1d" not in frame.columns:
        raise ValueError(f"{path} 必须包含 trade_date / ret_1d 两列")
    frame = frame.with_columns(
        pl.col("trade_date")
        .cast(pl.Utf8)
        .str.strptime(pl.Date, "%Y-%m-%d", strict=False),
        pl.col("ret_1d").cast(pl.Float64),
    )
    return frame.drop_nulls(["trade_date", "ret_1d"]).sort("trade_date")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s | %(message)s",
    )

    try:
        profile = load_profile(profile=args.profile)
        returns = _read_returns(args.returns)
        if returns.is_empty():
            raise ValueError("收益文件为空")
        start = returns["trade_date"].min()
        end = returns["trade_date"].max()

        spec = RunSpec(
            run_id=args.run_id,
            strategy_id=args.strategy_id,
            strategy_name=args.strategy_name,
            benchmark_id=args.benchmark_id,
            start_date=start if isinstance(start, date) else None,
            end_date=end if isinstance(end, date) else None,
            initial_capital=args.initial_capital,
            cost_model=args.cost_model,
        )

        with backtest_connection(profile) as con:
            require_backtest_schema(con)
            register_run(con, spec)
            try:
                write_nav(con, args.run_id, returns, benchmark_id=args.benchmark_id)
                metrics = compute_metrics(con, args.run_id)
            except Exception as exc:
                mark_run_failed(con, args.run_id, str(exc))
                raise
            LOG.info("完成 run_id=%s metrics=%s", args.run_id, sorted(metrics))
    except Exception as exc:  # noqa: BLE001 - CLI 顶层
        LOG.error("失败：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
