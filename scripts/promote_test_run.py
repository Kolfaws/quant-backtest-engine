#!/usr/bin/env python3
"""promote_test_run.py — 将测试库中的成功 run 发布到正式 backtest 库。

默认 dry-run；``--apply`` 才写正式库。正式库必须已有对应 benchmark/baseline。
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import duckdb
from quant_schema.constants import BT_COMPUTE_LOG, BT_NAV, BT_RUN_METRICS, BT_RUNS

from backtest_engine.core.config import load_profile


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _scalar(con: duckdb.DuckDBPyConnection, sql: str, params: list[object]) -> Any:
    rows = con.execute(sql, params).fetchall()
    return None if not rows else rows[0][0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="测试 run -> 正式 backtest 库")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    prod_cfg = load_profile(profile="production")
    test_cfg = load_profile(profile="test")
    test_path = test_cfg.backtest_db_path
    prod_path = prod_cfg.backtest_db_path
    if test_path.resolve() == prod_path.resolve():
        print("测试库与正式库路径相同，拒绝执行", file=sys.stderr)
        return 2
    if not test_path.is_file() or not prod_path.is_file():
        print("测试库或正式库不存在", file=sys.stderr)
        return 2

    prod = duckdb.connect(str(prod_path))
    try:
        prod.execute(f"ATTACH '{_quote(str(test_path))}' AS test_src (READ_ONLY)")
        try:
            row = prod.execute(
                f'SELECT status, benchmark_id FROM test_src."{BT_RUNS}" '
                "WHERE run_id = ?",
                [args.run_id],
            ).fetchall()
            if not row:
                print(f"测试库没有 run_id={args.run_id}", file=sys.stderr)
                return 1
            status, benchmark_id = str(row[0][0]), str(row[0][1])
            if status != "success":
                print(f"run 状态为 {status}，不是 success", file=sys.stderr)
                return 1
            exists = _scalar(
                prod,
                'SELECT 1 FROM "bt_benchmarks" WHERE benchmark_id = ? LIMIT 1',
                [benchmark_id],
            )
            if not exists:
                print(
                    f"正式库缺少 benchmark={benchmark_id}，请先发布基准",
                    file=sys.stderr,
                )
                return 1

            nav_rows = int(
                _scalar(
                    prod,
                    f'SELECT count(*) FROM test_src."{BT_NAV}" WHERE run_id = ?',
                    [args.run_id],
                )
                or 0
            )
            metric_rows = int(
                _scalar(
                    prod,
                    f'SELECT count(*) FROM test_src."{BT_RUN_METRICS}" WHERE run_id = ?',
                    [args.run_id],
                )
                or 0
            )
            print(f"run_id={args.run_id} success；nav={nav_rows} metrics={metric_rows}")
            if not args.apply:
                print("dry-run：未写入。确认后加 --apply")
                return 0

            prod.execute("BEGIN TRANSACTION")
            try:
                for table in (BT_NAV, BT_RUN_METRICS, BT_COMPUTE_LOG, BT_RUNS):
                    prod.execute(
                        f'DELETE FROM "{table}" WHERE run_id = ?',
                        [args.run_id],
                    )
                prod.execute(
                    f'INSERT INTO "{BT_RUNS}" SELECT * FROM test_src."{BT_RUNS}" '
                    "WHERE run_id = ?",
                    [args.run_id],
                )
                prod.execute(
                    f'INSERT INTO "{BT_NAV}" SELECT * FROM test_src."{BT_NAV}" '
                    "WHERE run_id = ?",
                    [args.run_id],
                )
                prod.execute(
                    f'INSERT INTO "{BT_RUN_METRICS}" '
                    f'SELECT * FROM test_src."{BT_RUN_METRICS}" WHERE run_id = ?',
                    [args.run_id],
                )
                prod.execute(
                    f'INSERT INTO "{BT_COMPUTE_LOG}" '
                    f'SELECT * FROM test_src."{BT_COMPUTE_LOG}" WHERE run_id = ?',
                    [args.run_id],
                )
                prod.execute("COMMIT")
            except Exception:
                prod.execute("ROLLBACK")
                raise
            print(f"已发布 run_id={args.run_id}")
        finally:
            prod.execute("DETACH test_src")
    finally:
        prod.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
