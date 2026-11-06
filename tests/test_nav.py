"""tests.test_nav — 净值写入、绩效指标、v_bt_nav_excess 视图。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import init_backtest_db as init
import polars as pl
import pytest

from backtest_engine.engine.metrics import (
    annualized_volatility,
    beta,
    max_drawdown,
    sharpe_ratio,
    summarize,
    total_return,
)
from backtest_engine.engine.nav import (
    RunSpec,
    compute_metrics,
    register_run,
    write_nav,
)


@pytest.fixture()
def con(tmp_path: Path):
    path = tmp_path / "backtest.duckdb"
    connection = init.connect(path)
    init.init_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def _returns() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_date": [
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 1, 4),
                date(2024, 1, 5),
            ],
            "ret_1d": [0.0, 0.10, -0.05, 0.02],
        },
        schema={"trade_date": pl.Date, "ret_1d": pl.Float64},
    )


def _insert_baseline(con) -> None:
    frame = pl.DataFrame(
        {
            "benchmark_id": ["BI_TEST"] * 4,
            "trade_date": [
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 1, 4),
                date(2024, 1, 5),
            ],
            "close": [100.0, 105.0, 100.0, 110.0],
            "ret_1d": [0.0, 0.05, -0.0476190476, 0.10],
            "nav": [1.0, 1.05, 1.0, 1.10],
            "base_date": [date(2024, 1, 2)] * 4,
            "base_value": [100.0] * 4,
            "kind": ["external"] * 4,
            "category": ["index"] * 4,
            "source": ["test"] * 4,
            "run_id": ["baseline-run"] * 4,
            "updated_at": [datetime(2024, 1, 1, tzinfo=UTC)] * 4,
        },
        schema={
            "benchmark_id": pl.Utf8,
            "trade_date": pl.Date,
            "close": pl.Float64,
            "ret_1d": pl.Float64,
            "nav": pl.Float64,
            "base_date": pl.Date,
            "base_value": pl.Float64,
            "kind": pl.Utf8,
            "category": pl.Utf8,
            "source": pl.Utf8,
            "run_id": pl.Utf8,
            "updated_at": pl.Datetime("us", "UTC"),
        },
    )
    con.register("_baseline", frame)
    try:
        con.execute(
            "INSERT INTO bt_baselines BY NAME SELECT * FROM _baseline "
            "ON CONFLICT (benchmark_id, trade_date) DO UPDATE SET nav = excluded.nav"
        )
    finally:
        con.unregister("_baseline")


def test_write_nav_and_metrics(con) -> None:
    spec = RunSpec(
        run_id="run-1",
        strategy_id="test",
        benchmark_id="BI_TEST",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 5),
    )
    register_run(con, spec)
    assert write_nav(con, "run-1", _returns(), benchmark_id="BI_TEST") == 4

    rows = con.execute(
        "SELECT trade_date, nav, cum_ret, drawdown FROM bt_nav "
        "WHERE run_id = 'run-1' ORDER BY trade_date"
    ).fetchall()
    assert [row[1] for row in rows] == pytest.approx([1.0, 1.1, 1.045, 1.0659])
    assert rows[2][2] == pytest.approx(0.045)
    assert rows[2][3] == pytest.approx(1.045 / 1.1 - 1)

    _insert_baseline(con)
    metrics = compute_metrics(con, "run-1")
    assert metrics["total_return"] == pytest.approx(0.0659)
    assert metrics["benchmark_total_return"] == pytest.approx(0.10)
    assert metrics["excess_total_return"] == pytest.approx(-0.0341)
    assert "information_ratio" in metrics

    run = con.execute(
        "SELECT status, benchmark_id FROM bt_runs WHERE run_id = 'run-1'"
    ).fetchall()[0]
    assert run[0] == "success"
    assert run[1] == "BI_TEST"

    stored = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT metric_name, metric_value FROM bt_run_metrics WHERE run_id = 'run-1'"
        ).fetchall()
    }
    assert stored["sharpe_ratio"] == pytest.approx(metrics["sharpe_ratio"])

    view = con.execute(
        "SELECT nav, benchmark_nav, excess_nav FROM v_bt_nav_excess "
        "WHERE run_id = 'run-1' ORDER BY trade_date"
    ).fetchall()
    assert view[-1][2] == pytest.approx(1.0659 / 1.10)


def test_idempotent_write(con) -> None:
    spec = RunSpec(run_id="run-1", strategy_id="test")
    register_run(con, spec)
    write_nav(con, "run-1", _returns())
    write_nav(con, "run-1", _returns().with_columns(pl.lit(0.2).alias("ret_1d")))
    rows = con.execute("SELECT count(*) FROM bt_nav WHERE run_id = 'run-1'").fetchall()
    assert rows[0][0] == 4


def test_metrics_helpers() -> None:
    assert total_return([0.1, -0.1]) == pytest.approx(-0.01)
    assert max_drawdown([1.0, 0.9, 1.2, 0.6]) == pytest.approx(0.6 / 1.2 - 1)
    assert sharpe_ratio([0.0, 0.0, 0.0]) is None
    assert annualized_volatility([0.01] * 10) == pytest.approx(0.0, abs=1e-12)
    assert beta([0.10, 0.20, 0.30], [0.05, 0.10, 0.15]) == pytest.approx(2.0)


def test_summarize_with_benchmark() -> None:
    returns = [0.0, 0.10, -0.05]
    navs = [1.0, 1.10, 1.045]
    benchmark = [0.0, 0.05, 0.0]
    result = summarize(returns, navs, benchmark_returns=benchmark)
    assert result["total_return"] == pytest.approx(0.045)
    assert result["benchmark_total_return"] == pytest.approx(0.05)
    assert result["excess_total_return"] == pytest.approx(-0.005)
    assert result["beta"] == pytest.approx(2.5)
    assert result["tracking_error"] is not None
