#!/usr/bin/env python3
"""demo_run.py — 用 market.duckdb 的 5 只核心票生成一个等权策略净值。

用途：
* 验证 ``backtest_engine`` 能只读打开 market.duckdb，并把结果写进 backtest.duckdb；
* 验证 ``bt_nav`` 与 ``v_bt_nav_excess`` 能和 quant-baseline 的 ``bt_baselines`` 对上；
* 这不是策略研究代码，只是端到端 smoke test。

用法：
    python scripts/demo_run.py --run-id demo_eqw --benchmark-id BI_EQW_CORE
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

import polars as pl

from backtest_engine.core.config import load_profile
from backtest_engine.core.db import (
    backtest_connection,
    market_connection,
    require_backtest_schema,
)
from backtest_engine.engine.nav import (
    RunSpec,
    compute_metrics,
    mark_run_failed,
    register_run,
    write_nav,
)

LOG = logging.getLogger("demo_run")
SYMBOLS = ("600519.SH", "000858.SZ", "600036.SH", "000001.SZ", "601318.SH")


def _load_returns(con, start: date, end: date) -> pl.DataFrame:
    placeholders = ", ".join("?" for _ in SYMBOLS)
    rows = con.execute(
        f"""
        SELECT trade_date, avg(pct_chg) / 100.0 AS ret_1d
        FROM fact_daily
        WHERE ts_code IN ({placeholders}) AND trade_date BETWEEN ? AND ?
        GROUP BY trade_date ORDER BY trade_date
        """,
        [*SYMBOLS, start, end],
    ).fetchall()
    if not rows:
        raise RuntimeError("market.duckdb 没有策略所需行情")
    frame = pl.DataFrame(
        {"trade_date": [row[0] for row in rows], "ret_1d": [row[1] for row in rows]},
        schema={"trade_date": pl.Date, "ret_1d": pl.Float64},
    )
    # 首个交易日净值归一为 1.0
    return frame.with_columns(
        pl.when(pl.col("trade_date") == pl.col("trade_date").min())
        .then(0.0)
        .otherwise(pl.col("ret_1d"))
        .alias("ret_1d")
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demo_run.py")
    parser.add_argument("--run-id", default="demo_eqw_core")
    parser.add_argument("--benchmark-id", default="BI_EQW_CORE")
    parser.add_argument("--start", default="2015-01-05")
    parser.add_argument("--end", default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s | %(message)s",
    )
    profile = load_profile(profile=args.profile)
    start = date.fromisoformat(args.start)
    try:
        with market_connection(profile) as market_con:
            end = (
                date.fromisoformat(args.end)
                if args.end
                else market_con.execute(
                    "SELECT max(trade_date) FROM fact_daily"
                ).fetchall()[0][0]
            )
            returns = _load_returns(market_con, start, end)

        with backtest_connection(profile) as con:
            require_backtest_schema(con)
            spec = RunSpec(
                run_id=args.run_id,
                strategy_id="demo_equal_weight",
                strategy_name="Demo equal-weight 5",
                benchmark_id=args.benchmark_id,
                start_date=returns["trade_date"][0],
                end_date=returns["trade_date"][-1],
                params_json='{"symbols": "5 core", "rebalance": "daily"}',
            )
            register_run(con, spec)
            try:
                write_nav(con, args.run_id, returns, benchmark_id=args.benchmark_id)
                metrics = compute_metrics(con, args.run_id)
            except Exception as exc:
                mark_run_failed(con, args.run_id, str(exc))
                raise
            LOG.info(
                "demo 完成 run_id=%s total=%.4f annual=%.4f sharpe=%.3f mdd=%.4f excess=%.4f",
                args.run_id,
                metrics.get("total_return", float("nan")),
                metrics.get("annualized_return", float("nan")),
                metrics.get("sharpe_ratio", float("nan")),
                metrics.get("max_drawdown", float("nan")),
                metrics.get("excess_total_return", float("nan")),
            )
    except Exception as exc:  # noqa: BLE001
        LOG.error("demo 失败：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
