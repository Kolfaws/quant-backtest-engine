"""sync_test_backtest.py — 把正式基准复制到测试 backtest 库。

只同步基准定义/点位/权重，不复制 bt_runs 与净值；测试 run 由测试库独立生成。
"""

from __future__ import annotations

import argparse
import sys

import duckdb
from quant_schema.constants import BT_BASELINE_WEIGHTS, BT_BASELINES, BT_BENCHMARKS

from backtest_engine.core.config import load_profile


def _quote(value: str) -> str:
    return value.replace("'", "''")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="正式基准 -> 测试 backtest 库")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    prod_cfg = load_profile(profile="production")
    test_cfg = load_profile(profile="test")
    prod_path, test_path = prod_cfg.backtest_db_path, test_cfg.backtest_db_path
    if prod_path.resolve() == test_path.resolve():
        print("正式库与测试库路径相同，拒绝执行", file=sys.stderr)
        return 2
    if not prod_path.is_file() or not test_path.is_file():
        print("正式库或测试库不存在", file=sys.stderr)
        return 2

    tables = (BT_BENCHMARKS, BT_BASELINES, BT_BASELINE_WEIGHTS)
    test = duckdb.connect(str(test_path))
    try:
        test.execute(f"ATTACH '{_quote(str(prod_path))}' AS prod_src (READ_ONLY)")
        try:
            for name in tables:
                count = int(
                    test.execute(f'SELECT count(*) FROM prod_src."{name}"').fetchall()[
                        0
                    ][0]
                )
                print(f"{name}: {count}")
            if not args.apply:
                print("dry-run：未写入。确认后加 --apply")
                return 0
            test.execute("BEGIN TRANSACTION")
            try:
                for name in tables:
                    test.execute(f'DELETE FROM "{name}"')
                    test.execute(
                        f'INSERT INTO "{name}" SELECT * FROM prod_src."{name}"'
                    )
                test.execute("COMMIT")
            except Exception:
                test.execute("ROLLBACK")
                raise
            print(f"已同步 {len(tables)} 张基准表到测试库")
        finally:
            test.execute("DETACH prod_src")
    finally:
        test.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
