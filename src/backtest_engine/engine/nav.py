"""engine.nav — 策略净值的登记、写入与绩效计算。

写入约定：

* ``bt_nav`` 是**唯一**由 backtest_engine 写的行情型表，主键 ``(run_id, trade_date)``；
* ``nav`` 为策略归一净值（首个交易日通常为 1.0）；
* 超额计算在 ``v_bt_nav_excess`` 视图里，本模块只负责把指标物化到
  ``bt_run_metrics``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

import polars as pl
from quant_schema.constants import (
    BT_NAV,
    BT_NAV_KEYS,
    BT_RUN_METRICS,
    BT_RUN_METRICS_KEYS,
    BT_RUNS,
    BT_RUNS_KEYS,
)

from .metrics import summarize

LOG = logging.getLogger("backtest_engine.nav")

ENGINE_VERSION: Final[str] = "0.1.0"


@dataclass(frozen=True, slots=True)
class RunSpec:
    """一次回测的登记信息（对应 ``bt_runs`` 一行）。"""

    run_id: str
    strategy_id: str
    strategy_name: str | None = None
    benchmark_id: str | None = None
    params_json: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    initial_capital: float = 1_000_000.0
    cost_model: str | None = None
    engine_version: str = ENGINE_VERSION
    config_hash: str | None = None


# ---------------------------------------------------------------------------
# 写入辅助
# ---------------------------------------------------------------------------
def _upsert(con, table: str, frame: pl.DataFrame, keys: tuple[str, ...]) -> int:
    """按主键 upsert；只更新 DataFrame 中出现的列。"""
    if frame.is_empty():
        return 0
    allowed = [str(row[0]) for row in con.execute(f'DESCRIBE "{table}"').fetchall()]
    columns = [name for name in allowed if name in frame.columns]
    missing = [key for key in keys if key not in columns]
    if missing:
        raise ValueError(f"{table} 缺少主键列：{missing}")

    frame = frame.select(columns).unique(subset=list(keys), keep="last")
    update_columns = [name for name in columns if name not in keys]
    conflict = (
        "DO UPDATE SET "
        + ", ".join(f'"{name}" = excluded."{name}"' for name in update_columns)
        if update_columns
        else "DO NOTHING"
    )
    view_name = "_bt_batch"
    con.register(view_name, frame)
    try:
        key_sql = ", ".join(f'"{key}"' for key in keys)
        con.execute(
            f'INSERT INTO "{table}" BY NAME SELECT * FROM {view_name} '
            f"ON CONFLICT ({key_sql}) {conflict}"
        )
    finally:
        con.unregister(view_name)
    return frame.height


def register_run(con, spec: RunSpec) -> None:
    """登记/重置一次回测（幂等：同一 run_id 覆盖配置并置为 running）。"""
    frame = pl.DataFrame(
        {
            "run_id": [spec.run_id],
            "strategy_id": [spec.strategy_id],
            "strategy_name": [spec.strategy_name],
            "benchmark_id": [spec.benchmark_id],
            "params_json": [spec.params_json],
            "start_date": [spec.start_date],
            "end_date": [spec.end_date],
            "initial_capital": [float(spec.initial_capital)],
            "cost_model": [spec.cost_model],
            "status": ["running"],
            "engine_version": [spec.engine_version],
            "config_hash": [spec.config_hash],
        }
    )
    _upsert(con, BT_RUNS, frame, BT_RUNS_KEYS)
    LOG.info("登记回测 run_id=%s strategy=%s", spec.run_id, spec.strategy_id)


def write_nav(
    con,
    run_id: str,
    returns: pl.DataFrame,
    *,
    benchmark_id: str | None = None,
) -> int:
    """把日收益序列转成净值并幂等写入 ``bt_nav``。

    Args:
        returns: 至少包含 ``trade_date``、``ret_1d`` 两列；首行收益通常为 0。
        benchmark_id: 基准代码；为空时从 returns 的同名列读取。

    Returns:
        写入行数。
    """
    required = {"trade_date", "ret_1d"}
    missing = required - set(returns.columns)
    if missing:
        raise ValueError(f"returns 缺列 {sorted(missing)}")

    frame = (
        returns.select(
            pl.col("trade_date").cast(pl.Date),
            pl.col("ret_1d").cast(pl.Float64),
            (
                pl.col("benchmark_id").cast(pl.Utf8)
                if "benchmark_id" in returns.columns
                else pl.lit(benchmark_id).cast(pl.Utf8)
            ).alias("benchmark_id"),
        )
        .drop_nulls(["trade_date", "ret_1d"])
        .unique(subset=["trade_date"], keep="last")
        .sort("trade_date")
    )
    if frame.is_empty():
        return 0

    frame = (
        frame.with_columns((1.0 + pl.col("ret_1d")).cum_prod().alias("nav"))
        .with_columns((pl.col("nav") - 1.0).alias("cum_ret"))
        .with_columns((pl.col("nav") / pl.col("nav").cum_max() - 1.0).alias("drawdown"))
        .with_columns(pl.lit(run_id).alias("run_id"))
        .select(
            "run_id",
            "trade_date",
            "nav",
            "ret_1d",
            "cum_ret",
            "drawdown",
            "benchmark_id",
        )
    )
    rows = _upsert(con, BT_NAV, frame, BT_NAV_KEYS)
    LOG.info("写入净值 run_id=%s rows=%d", run_id, rows)
    return rows


def _load_nav(con, run_id: str) -> tuple[list[date], list[float], list[float | None]]:
    rows = con.execute(
        "SELECT trade_date, nav, ret_1d FROM bt_nav WHERE run_id = ? ORDER BY trade_date",
        [run_id],
    ).fetchall()
    dates = [row[0] for row in rows]
    navs = [float(row[1]) for row in rows]
    rets = [None if row[2] is None else float(row[2]) for row in rows]
    return dates, navs, rets


def _load_benchmark(
    con, benchmark_id: str, start: date, end: date
) -> dict[date, float]:
    rows = con.execute(
        "SELECT trade_date, ret_1d FROM bt_baselines "
        "WHERE benchmark_id = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [benchmark_id, start, end],
    ).fetchall()
    return {row[0]: float(row[1]) for row in rows if row[1] is not None}


def compute_metrics(con, run_id: str) -> dict[str, float]:
    """计算并物化绩效指标；基准存在时同时算超额、跟踪误差、信息比率、beta。"""
    dates, navs, rets = _load_nav(con, run_id)
    if not dates:
        raise ValueError(f"bt_nav 中没有 run_id={run_id} 的记录")

    rows = con.execute(
        "SELECT benchmark_id FROM bt_runs WHERE run_id = ?", [run_id]
    ).fetchall()
    benchmark_id = None
    if rows and rows[0][0]:
        benchmark_id = str(rows[0][0])
    else:
        found = con.execute(
            "SELECT benchmark_id FROM bt_nav WHERE run_id = ? AND benchmark_id IS NOT NULL LIMIT 1",
            [run_id],
        ).fetchall()
        benchmark_id = str(found[0][0]) if found else None

    benchmark_returns = None
    if benchmark_id:
        mapping = _load_benchmark(con, benchmark_id, dates[0], dates[-1])
        benchmark_returns = [mapping.get(day) for day in dates]
        if not any(value is not None for value in benchmark_returns):
            LOG.warning(
                "基准 %s 在 %s..%s 无数据，跳过超额指标",
                benchmark_id,
                dates[0],
                dates[-1],
            )
            benchmark_returns = None

    metrics = summarize(rets, navs, benchmark_returns=benchmark_returns)
    metrics = {name: value for name, value in metrics.items() if value is not None}

    frame = pl.DataFrame(
        {
            "run_id": [run_id] * len(metrics),
            "metric_name": list(metrics),
            "metric_value": [float(value) for value in metrics.values()],
        }
    )
    _upsert(con, BT_RUN_METRICS, frame, BT_RUN_METRICS_KEYS)

    con.execute(
        "UPDATE bt_runs SET status = 'success', finished_at = ? WHERE run_id = ?",
        [datetime.now(UTC), run_id],
    )
    LOG.info("回测完成 run_id=%s metrics=%d", run_id, len(metrics))
    return metrics


def mark_run_failed(con, run_id: str, error: str) -> None:
    """把回测标记为 failed；异常摘要只进日志，不污染配置字段。"""
    con.execute(
        "UPDATE bt_runs SET status = 'failed', finished_at = ? WHERE run_id = ?",
        [datetime.now(UTC), run_id],
    )
    LOG.error("回测失败 run_id=%s error=%s", run_id, error)


__all__ = [
    "RunSpec",
    "compute_metrics",
    "mark_run_failed",
    "register_run",
    "write_nav",
]
