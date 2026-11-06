#!/usr/bin/env python3
"""init_test_db.py — 初始化 backtest_test.duckdb。

测试库与正式库使用同一套 DDL；路径来自 ``BACKTEST_TEST_DB_PATH``。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import init_backtest_db as init

LOG = logging.getLogger("init_test_db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化测试 backtest 库")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(message)s")

    raw = args.db or os.getenv("BACKTEST_TEST_DB_PATH")
    if not raw:
        LOG.error("未配置 BACKTEST_TEST_DB_PATH")
        return 2
    db_path = Path(raw)
    prod = os.getenv("BACKTEST_DB_PATH")
    if prod and Path(prod).resolve() == db_path.resolve():
        LOG.error("测试库路径不能与正式库相同：%s", db_path)
        return 2

    with init.database(db_path) as con:
        init.init_schema(con)
        LOG.info(
            "测试库初始化完成 db=%s version=%s", db_path, init.installed_version(con)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
