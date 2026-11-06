"""engine.metrics — 基于 Polars 的向量化绩效指标。

输入统一为“日频收益序列 / 净值序列”，年化按 252 个交易日。
所有公开函数对空序列返回 ``None``，不抛异常；调用方决定是否跳过。

实现约定：

* 序列 -> ``pl.Series``，用 ``product`` / ``std`` / ``cum_max`` 等表达式计算；
* 不写 Python 循环、不用 ``statistics``；
* 只有 beta 的协方差矩阵用 numpy（``np.cov``），属于底层数学聚合的例外。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final

import numpy as np
import polars as pl

TRADING_DAYS: Final[int] = 252


def _as_series(
    values: Sequence[float | None] | pl.Series, name: str = "value"
) -> pl.Series:
    """转成 ``Float64`` Series，并剔除 null / NaN / inf。"""
    series = (
        values.clone().cast(pl.Float64)
        if isinstance(values, pl.Series)
        else pl.Series(name, list(values), dtype=pl.Float64)
    )
    cleaned = series.drop_nulls()
    return cleaned.filter(cleaned.is_finite())


def total_return(returns: Sequence[float | None] | pl.Series) -> float | None:
    series = _as_series(returns)
    if series.is_empty():
        return None
    return float((1.0 + series).product() - 1.0)


def annualized_return(
    returns: Sequence[float | None] | pl.Series, periods: int = TRADING_DAYS
) -> float | None:
    series = _as_series(returns)
    if series.is_empty():
        return None
    gross = float((1.0 + series).product())
    if gross <= 0:
        return -1.0
    return gross ** (periods / series.len()) - 1.0


def annualized_volatility(
    returns: Sequence[float | None] | pl.Series, periods: int = TRADING_DAYS
) -> float | None:
    series = _as_series(returns)
    if series.len() < 2:
        return None
    return float(series.std(ddof=1)) * math.sqrt(periods)


def sharpe_ratio(
    returns: Sequence[float | None] | pl.Series, periods: int = TRADING_DAYS
) -> float | None:
    series = _as_series(returns)
    if series.len() < 2:
        return None
    std = float(series.std(ddof=1))
    if std == 0:
        return None
    return float(series.mean()) / std * math.sqrt(periods)


def max_drawdown(navs: Sequence[float | None] | pl.Series) -> float | None:
    series = _as_series(navs)
    series = series.filter(series > 0)
    if series.is_empty():
        return None
    drawdown = series / series.cum_max() - 1.0
    return float(drawdown.min())


def calmar_ratio(
    returns: Sequence[float | None] | pl.Series,
    navs: Sequence[float | None] | pl.Series,
    periods: int = TRADING_DAYS,
) -> float | None:
    annual = annualized_return(returns, periods)
    drawdown = max_drawdown(navs)
    if annual is None or drawdown is None or drawdown == 0:
        return None
    return annual / abs(drawdown)


def win_rate(returns: Sequence[float | None] | pl.Series) -> float | None:
    series = _as_series(returns)
    active = series.filter(series != 0)
    if active.is_empty():
        return None
    return float((active > 0).sum()) / active.len()


def beta(
    returns: Sequence[float | None] | pl.Series,
    benchmark: Sequence[float | None] | pl.Series,
) -> float | None:
    strategy = (
        returns.clone().cast(pl.Float64)
        if isinstance(returns, pl.Series)
        else pl.Series("strategy", list(returns), dtype=pl.Float64)
    )
    bench = (
        benchmark.clone().cast(pl.Float64)
        if isinstance(benchmark, pl.Series)
        else pl.Series("benchmark", list(benchmark), dtype=pl.Float64)
    )
    frame = (
        pl.DataFrame({"strategy": strategy, "benchmark": bench})
        .drop_nulls()
        .filter(pl.col("strategy").is_finite() & pl.col("benchmark").is_finite())
    )
    if frame.height < 2:
        return None
    strategy_values = frame["strategy"].to_numpy()
    benchmark_values = frame["benchmark"].to_numpy()
    variance = float(np.var(benchmark_values, ddof=1))
    if variance == 0:
        return None
    covariance = float(np.cov(strategy_values, benchmark_values, ddof=1)[0, 1])
    return covariance / variance


def tracking_error(
    active_returns: Sequence[float | None] | pl.Series,
    periods: int = TRADING_DAYS,
) -> float | None:
    return annualized_volatility(active_returns, periods)


def information_ratio(
    active_returns: Sequence[float | None] | pl.Series,
    periods: int = TRADING_DAYS,
) -> float | None:
    series = _as_series(active_returns)
    if series.len() < 2:
        return None
    te = float(series.std(ddof=1)) * math.sqrt(periods)
    if te == 0:
        return None
    return float(series.mean()) * periods / te


def summarize(
    returns: Sequence[float | None] | pl.Series,
    navs: Sequence[float | None] | pl.Series,
    *,
    benchmark_returns: Sequence[float | None] | pl.Series | None = None,
) -> dict[str, float | None]:
    """一次性计算常用指标；带基准时额外给超额与信息比率。"""
    metrics: dict[str, float | None] = {
        "total_return": total_return(returns),
        "annualized_return": annualized_return(returns),
        "annualized_volatility": annualized_volatility(returns),
        "sharpe_ratio": sharpe_ratio(returns),
        "max_drawdown": max_drawdown(navs),
        "calmar_ratio": calmar_ratio(returns, navs),
        "win_rate": win_rate(returns),
    }
    if benchmark_returns is None:
        return metrics

    strategy = (
        returns.clone().cast(pl.Float64)
        if isinstance(returns, pl.Series)
        else pl.Series("strategy", list(returns), dtype=pl.Float64)
    )
    bench = (
        benchmark_returns.clone().cast(pl.Float64)
        if isinstance(benchmark_returns, pl.Series)
        else pl.Series("benchmark", list(benchmark_returns), dtype=pl.Float64)
    )
    aligned = (
        pl.DataFrame({"strategy": strategy, "benchmark": bench})
        .drop_nulls()
        .filter(pl.col("strategy").is_finite() & pl.col("benchmark").is_finite())
        .with_columns((pl.col("strategy") - pl.col("benchmark")).alias("active"))
    )
    if aligned.is_empty():
        return metrics

    strategy_series = aligned["strategy"]
    benchmark_series = aligned["benchmark"]
    active_series = aligned["active"]
    metrics.update(
        {
            "benchmark_total_return": total_return(benchmark_series),
            "excess_total_return": (total_return(strategy_series) or 0.0)
            - (total_return(benchmark_series) or 0.0),
            "excess_annualized_return": (
                annualized_return(strategy_series) or 0.0
            )
            - (annualized_return(benchmark_series) or 0.0),
            "tracking_error": tracking_error(active_series),
            "information_ratio": information_ratio(active_series),
            "beta": beta(strategy_series, benchmark_series),
        }
    )
    return metrics


__all__ = [
    "TRADING_DAYS",
    "annualized_return",
    "annualized_volatility",
    "beta",
    "calmar_ratio",
    "information_ratio",
    "max_drawdown",
    "sharpe_ratio",
    "summarize",
    "total_return",
    "tracking_error",
    "win_rate",
]
