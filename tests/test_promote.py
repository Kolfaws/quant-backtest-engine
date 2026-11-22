"""tests.test_promote — promote_test_run：只读 dry-run 与发布前置校验。

全部使用 ``tmp_path`` 下的 test/prod 临时库；通过 env 指到临时路径，
不触碰正式库。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import init_backtest_db as init
import promote_test_run as promote
import pytest

RUN_ID = "run-promote"
BENCHMARK = "BI_TEST"
DATES = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]


def _connect(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path))


def _init_db(path: Path) -> None:
    with init.database(path) as con:
        init.init_schema(con)


def _seed_test_run(
    path: Path,
    *,
    status: str = "success",
    benchmark_id: str | None = BENCHMARK,
    nav_dates: list[date] | None = None,
) -> None:
    dates = DATES if nav_dates is None else nav_dates
    con = _connect(path)
    try:
        con.execute(
            "INSERT INTO bt_runs "
            "(run_id, strategy_id, benchmark_id, start_date, end_date, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [RUN_ID, "strategy", benchmark_id, dates[0], dates[-1], status],
        )
        for index, day in enumerate(dates):
            con.execute(
                "INSERT INTO bt_nav "
                "(run_id, trade_date, nav, ret_1d, cum_ret, drawdown, benchmark_id) "
                "VALUES (?, ?, ?, ?, ?, 0.0, ?)",
                [
                    RUN_ID,
                    day,
                    1.0 + 0.01 * index,
                    0.0 if index == 0 else 0.01,
                    0.01 * index,
                    benchmark_id,
                ],
            )
        con.execute(
            "INSERT INTO bt_run_metrics (run_id, metric_name, metric_value) "
            "VALUES (?, 'total_return', 0.02)",
            [RUN_ID],
        )
    finally:
        con.close()


def _seed_prod_benchmark(
    path: Path,
    *,
    dates: list[date] | None = None,
    with_definition: bool = True,
) -> None:
    baseline_dates = DATES if dates is None else dates
    con = _connect(path)
    try:
        if with_definition:
            con.execute(
                "INSERT INTO bt_benchmarks (benchmark_id, name, kind) "
                "VALUES (?, 'test benchmark', 'external')",
                [BENCHMARK],
            )
        for index, day in enumerate(baseline_dates):
            con.execute(
                "INSERT INTO bt_baselines "
                "(benchmark_id, trade_date, close, ret_1d, nav, kind, run_id) "
                "VALUES (?, ?, ?, ?, ?, 'external', 'baseline-run')",
                [
                    BENCHMARK,
                    day,
                    100.0 + index,
                    0.0 if index == 0 else 0.01,
                    1.0 + 0.01 * index,
                ],
            )
    finally:
        con.close()


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    prod_db = tmp_path / "backtest.duckdb"
    test_db = tmp_path / "backtest_test.duckdb"
    _init_db(prod_db)
    _init_db(test_db)
    _seed_test_run(test_db)
    _seed_prod_benchmark(prod_db)
    monkeypatch.setenv("BACKTEST_DB_PATH", str(prod_db))
    monkeypatch.setenv("BACKTEST_TEST_DB_PATH", str(test_db))
    monkeypatch.setenv("MARKET_DB_PATH", str(tmp_path / "market.duckdb"))
    monkeypatch.setenv("MARKET_TEST_DB_PATH", str(tmp_path / "market_test.duckdb"))
    return prod_db, test_db


def _count(path: Path, table: str, where: str = "") -> int:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return int(con.execute(f'SELECT count(*) FROM "{table}" {where}').fetchone()[0])
    finally:
        con.close()


def test_dry_run_is_read_only_and_writes_nothing(
    dbs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prod_db, _ = dbs
    calls: list[tuple[str, bool]] = []
    real_connect = duckdb.connect

    def _record(path: str | Path, *, read_only: bool = False):
        calls.append((str(path), read_only))
        return real_connect(str(path), read_only=read_only)

    monkeypatch.setattr(promote, "_connect", _record)
    assert promote.main(["--run-id", RUN_ID]) == 0

    prod_calls = [read_only for path, read_only in calls if Path(path) == prod_db]
    assert prod_calls, "dry-run 应打开正式库"
    assert all(
        read_only for read_only in prod_calls
    ), "dry-run 不得以读写模式打开正式库"
    # 只读 dry-run 不应写入任何 run/nav/metrics
    assert _count(prod_db, "bt_runs") == 0
    assert _count(prod_db, "bt_nav") == 0
    assert _count(prod_db, "bt_run_metrics") == 0
    assert "dry-run" in capsys.readouterr().out


def test_apply_copies_three_tables_and_keeps_compute_log(
    dbs: tuple[Path, Path],
) -> None:
    prod_db, _ = dbs
    con = _connect(prod_db)
    try:
        # 正式库已有同 run 的旧 tail + 一条 baseline 拥有的 compute_log：前者被替换，后者不动
        con.execute(
            "INSERT INTO bt_runs (run_id, strategy_id, status) "
            "VALUES (?, 'old', 'success')",
            [RUN_ID],
        )
        con.execute(
            "INSERT INTO bt_nav (run_id, trade_date, nav) "
            "VALUES (?, DATE '2020-01-02', 9.9)",
            [RUN_ID],
        )
        con.execute(
            "INSERT INTO bt_compute_log "
            "(run_id, job_name, target_key, status, started_at) "
            "VALUES (?, 'baseline', ?, 'success', current_timestamp)",
            [RUN_ID, BENCHMARK],
        )
    finally:
        con.close()

    assert promote.main(["--run-id", RUN_ID, "--apply"]) == 0

    assert _count(prod_db, "bt_runs", f"WHERE run_id = '{RUN_ID}'") == 1
    assert _count(prod_db, "bt_nav", f"WHERE run_id = '{RUN_ID}'") == 3
    assert _count(prod_db, "bt_run_metrics", f"WHERE run_id = '{RUN_ID}'") == 1
    assert _count(prod_db, "bt_nav", "WHERE trade_date = DATE '2020-01-02'") == 0
    # 基准四表（bt_compute_log）仍归 quant-baseline，promotion 不改写
    assert _count(prod_db, "bt_compute_log") == 1


def test_missing_benchmark_id_rejected(
    dbs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, test_db = dbs
    con = _connect(test_db)
    try:
        con.execute("UPDATE bt_runs SET benchmark_id = NULL WHERE run_id = ?", [RUN_ID])
    finally:
        con.close()
    assert promote.main(["--run-id", RUN_ID]) == 1
    assert "benchmark_id" in capsys.readouterr().err


def test_missing_benchmark_definition_rejected(
    dbs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    prod_db, _ = dbs
    con = _connect(prod_db)
    try:
        con.execute("DELETE FROM bt_benchmarks")
    finally:
        con.close()
    assert promote.main(["--run-id", RUN_ID]) == 1
    assert "benchmark" in capsys.readouterr().err


def test_incomplete_baseline_coverage_rejected(
    dbs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    prod_db, _ = dbs
    con = _connect(prod_db)
    try:
        con.execute(f"DELETE FROM bt_baselines WHERE trade_date = DATE '{DATES[-1]}'")
    finally:
        con.close()
    assert promote.main(["--run-id", RUN_ID]) == 1
    err = capsys.readouterr().err
    assert "bt_baselines" in err
    assert "覆盖" in err


def test_schema_version_mismatch_rejected(
    dbs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    prod_db, _ = dbs
    con = _connect(prod_db)
    try:
        con.execute(
            "UPDATE meta_schema_version SET version = '9.9.9' "
            "WHERE component = 'backtest'"
        )
    finally:
        con.close()
    assert promote.main(["--run-id", RUN_ID]) == 1
    assert "版本" in capsys.readouterr().err


def test_column_mismatch_rejected(
    dbs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    prod_db, _ = dbs
    con = _connect(prod_db)
    try:
        con.execute("ALTER TABLE bt_nav ADD COLUMN unexpected DOUBLE")
    finally:
        con.close()
    assert promote.main(["--run-id", RUN_ID]) == 1
    assert "列名" in capsys.readouterr().err
