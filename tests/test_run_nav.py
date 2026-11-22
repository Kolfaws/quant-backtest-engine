"""tests.test_run_nav — run_nav CLI：首日契约、原子重跑、失败回滚。

全部使用 ``tmp_path`` 下的临时 backtest 库；``load_profile`` 被替换为临时
``ProfileConfig``，不读真实库、不依赖 ``market.duckdb``。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import init_backtest_db as init
import pytest
import run_nav

from backtest_engine.core.config import ProfileConfig

RUN_ID = "cli-run"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "backtest.duckdb"
    with init.database(path) as con:
        init.init_schema(con)
    return path


@pytest.fixture()
def profile(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ProfileConfig:
    config = ProfileConfig(
        name="test",
        market_db_path=tmp_path / "market.duckdb",
        backtest_db_path=db_path,
        market_read_only=True,
        threads=2,
        memory_limit="1GB",
        temp_dir=None,
    )
    monkeypatch.setattr(run_nav, "load_profile", lambda **_: config)
    return config


def _write_csv(path: Path, rows: list[tuple[date, float]]) -> Path:
    lines = ["trade_date,ret_1d", *(f"{day},{ret}" for day, ret in rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _query(db_path: Path, sql: str, params: list[object] | None = None) -> list:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def _run(csv_path: Path, *extra: str) -> int:
    return run_nav.main(
        [
            "--run-id",
            RUN_ID,
            "--strategy-id",
            "test_strategy",
            "--returns",
            str(csv_path),
            *extra,
        ]
    )


def test_rejects_nonzero_first_day(db_path: Path, tmp_path: Path, profile) -> None:
    csv_path = _write_csv(
        tmp_path / "returns.csv",
        [(date(2024, 1, 2), 0.02), (date(2024, 1, 3), 0.01)],
    )
    assert _run(csv_path) == 1
    # 拒绝发生在写库前：库内没有 run/nav 痕迹
    assert _query(db_path, "SELECT count(*) FROM bt_runs")[0][0] == 0
    assert _query(db_path, "SELECT count(*) FROM bt_nav")[0][0] == 0


def test_allow_nonzero_first_day_sets_nav_one(
    db_path: Path, tmp_path: Path, profile
) -> None:
    csv_path = _write_csv(
        tmp_path / "returns.csv",
        [(date(2024, 1, 2), 0.02), (date(2024, 1, 3), 0.03)],
    )
    assert _run(csv_path, "--allow-nonzero-first-day") == 0
    rows = _query(
        db_path,
        "SELECT trade_date, nav, ret_1d FROM bt_nav "
        "WHERE run_id = ? ORDER BY trade_date",
        [RUN_ID],
    )
    assert rows[0] == (date(2024, 1, 2), pytest.approx(1.0), pytest.approx(0.0))
    assert rows[1][1] == pytest.approx(1.03)
    metrics = dict(
        _query(
            db_path,
            "SELECT metric_name, metric_value FROM bt_run_metrics WHERE run_id = ?",
            [RUN_ID],
        )
    )
    assert metrics["total_return"] == pytest.approx(0.03)


def test_rerun_short_window_has_no_stale_tail(
    db_path: Path, tmp_path: Path, profile
) -> None:
    full = _write_csv(
        tmp_path / "full.csv",
        [
            (date(2024, 1, 2), 0.0),
            (date(2024, 1, 3), 0.01),
            (date(2024, 1, 4), -0.01),
            (date(2024, 1, 5), 0.02),
        ],
    )
    assert _run(full) == 0
    assert (
        _query(db_path, "SELECT count(*) FROM bt_nav WHERE run_id = ?", [RUN_ID])[0][0]
        == 4
    )

    # 把上一轮时间戳改成旧值，验证重跑会重置 finished_at / computed_at
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "UPDATE bt_runs SET finished_at = TIMESTAMPTZ '2000-01-01 00:00:00+00' "
            "WHERE run_id = ?",
            [RUN_ID],
        )
        con.execute(
            "UPDATE bt_run_metrics SET computed_at = "
            "TIMESTAMPTZ '2000-01-01 00:00:00+00' WHERE run_id = ?",
            [RUN_ID],
        )
    finally:
        con.close()

    short = _write_csv(
        tmp_path / "short.csv",
        [(date(2024, 1, 2), 0.0), (date(2024, 1, 3), 0.01)],
    )
    assert _run(short) == 0

    nav_rows = _query(
        db_path,
        "SELECT trade_date FROM bt_nav WHERE run_id = ? ORDER BY trade_date",
        [RUN_ID],
    )
    assert nav_rows == [(date(2024, 1, 2),), (date(2024, 1, 3),)]
    finished_at, end_date, status = _query(
        db_path,
        "SELECT finished_at, end_date, status FROM bt_runs WHERE run_id = ?",
        [RUN_ID],
    )[0]
    assert finished_at is not None
    assert end_date == date(2024, 1, 3)
    assert status == "success"
    stale_metrics = _query(
        db_path,
        "SELECT count(*) FROM bt_run_metrics WHERE run_id = ? "
        "AND computed_at < TIMESTAMPTZ '2020-01-01 00:00:00+00'",
        [RUN_ID],
    )[0][0]
    assert stale_metrics == 0


def test_failed_rerun_rolls_back_and_marks_failed(
    db_path: Path,
    tmp_path: Path,
    profile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = _write_csv(
        tmp_path / "full.csv",
        [
            (date(2024, 1, 2), 0.0),
            (date(2024, 1, 3), 0.01),
            (date(2024, 1, 4), -0.01),
            (date(2024, 1, 5), 0.02),
        ],
    )
    assert _run(full) == 0

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("forced failure")

    monkeypatch.setattr(run_nav, "compute_metrics", _boom)
    short = _write_csv(
        tmp_path / "short.csv",
        [(date(2024, 1, 2), 0.0), (date(2024, 1, 3), 0.01)],
    )
    assert _run(short) == 1

    # 事务回滚：旧 4 行净值仍在（没有被半批 2 行覆盖）
    assert (
        _query(db_path, "SELECT count(*) FROM bt_nav WHERE run_id = ?", [RUN_ID])[0][0]
        == 4
    )
    status, finished_at = _query(
        db_path, "SELECT status, finished_at FROM bt_runs WHERE run_id = ?", [RUN_ID]
    )[0]
    assert status == "failed"
    assert finished_at is not None


def test_failed_first_run_records_failed_status(
    db_path: Path, tmp_path: Path, profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首次运行在建行后失败：事务回滚删掉半批数据，但仍补写 failed run 行。"""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("forced failure")

    monkeypatch.setattr(run_nav, "compute_metrics", _boom)
    csv_path = _write_csv(
        tmp_path / "returns.csv",
        [(date(2024, 1, 2), 0.0), (date(2024, 1, 3), 0.01)],
    )
    assert _run(csv_path) == 1

    status, finished_at = _query(
        db_path, "SELECT status, finished_at FROM bt_runs WHERE run_id = ?", [RUN_ID]
    )[0]
    assert status == "failed"
    assert finished_at is not None
    # rollback 后没有半批净值
    assert (
        _query(db_path, "SELECT count(*) FROM bt_nav WHERE run_id = ?", [RUN_ID])[0][0]
        == 0
    )
