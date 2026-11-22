#!/usr/bin/env python3
"""run_nav.py — 把策略日收益 CSV 写成 bt_runs / bt_nav / bt_run_metrics。

这是 backtest_engine 的“最小可用入口”：策略逻辑由 factor_research（或任何
上游）产出日收益，本脚本负责登记运行、落净值、算绩效与超额。

CSV 格式（表头必需）：
    trade_date,ret_1d
    2024-01-02,0.0
    2024-01-03,0.0123

首日契约：``bt_nav`` 首个交易日 ``nav`` 恒为 1.0、``ret_1d`` 恒为 0。
CSV 首行收益非 0 时默认**拒绝执行**；显式加 ``--allow-nonzero-first-day``
才由引擎置 0 并告警。重跑同一 ``--run-id`` 会整体替换旧 ``bt_nav`` /
``bt_run_metrics``（登记、净值、指标三写同一事务），失败回滚保留旧数据。

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

from backtest_engine.core.config import load_profile
from backtest_engine.core.db import (
    backtest_connection,
    require_backtest_schema,
)
from backtest_engine.core.execution import load_execution_config
from backtest_engine.engine.nav import (
    FIRST_RET_TOLERANCE,
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
        "--execution-config",
        type=Path,
        default=None,
        help="执行配置 YAML（configs/execution.yaml）；只做校验/审计记录",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    parser.add_argument(
        "--allow-nonzero-first-day",
        action="store_true",
        help="首行 ret_1d 非 0 时不报错，改为告警并由引擎置 0（首日 nav 仍为 1.0）",
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
    return (
        frame.drop_nulls(["trade_date", "ret_1d"])
        .unique(subset=["trade_date"], keep="last")
        .sort("trade_date")
    )


def _enforce_first_day(returns: pl.DataFrame, *, allow_nonzero: bool) -> None:
    """校验首行收益；默认拒绝非 0 首行，``allow_nonzero`` 时仅告警（由引擎置 0）。"""
    if returns.is_empty():
        raise ValueError("收益文件为空")
    first_ret = float(returns["ret_1d"][0])
    if abs(first_ret) <= FIRST_RET_TOLERANCE:
        return
    day = returns["trade_date"][0]
    message = (
        f"收益文件首日 {day} 的 ret_1d={first_ret:.10g} 非 0；"
        "bt_nav 契约要求首日 ret_1d=0、nav=1.0。请把首行收益置 0，"
        "或加 --allow-nonzero-first-day 由引擎置 0 并告警"
    )
    if allow_nonzero:
        LOG.warning("%s；已按 --allow-nonzero-first-day 置 0", message)
        return
    raise ValueError(message)


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
        _enforce_first_day(returns, allow_nonzero=args.allow_nonzero_first_day)
        start = returns["trade_date"].min()
        end = returns["trade_date"].max()
        execution = (
            load_execution_config(args.execution_config)
            if args.execution_config is not None
            else None
        )
        cost_model = execution.summary() if execution is not None else args.cost_model
        if execution is not None:
            LOG.info("execution config: %s", cost_model)

        spec = RunSpec(
            run_id=args.run_id,
            strategy_id=args.strategy_id,
            strategy_name=args.strategy_name,
            benchmark_id=args.benchmark_id,
            start_date=start if isinstance(start, date) else None,
            end_date=end if isinstance(end, date) else None,
            initial_capital=args.initial_capital,
            cost_model=cost_model,
        )

        with backtest_connection(profile) as con:
            require_backtest_schema(con)
            # 登记 + 写净值 + 算指标三写同事务：失败整体回滚，不留半批数据。
            con.execute("BEGIN TRANSACTION")
            try:
                register_run(con, spec)
                write_nav(con, args.run_id, returns, benchmark_id=args.benchmark_id)
                metrics = compute_metrics(con, args.run_id)
                con.execute("COMMIT")
            except Exception as exc:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    LOG.debug("回滚失败（事务可能已结束）", exc_info=True)
                try:
                    mark_run_failed(con, args.run_id, str(exc), spec=spec)
                except Exception:
                    LOG.exception("记录 failed 状态失败 run_id=%s", args.run_id)
                raise
            LOG.info("完成 run_id=%s metrics=%s", args.run_id, sorted(metrics))
    except Exception as exc:  # noqa: BLE001 - CLI 顶层
        LOG.error("失败：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
