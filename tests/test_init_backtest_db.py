"""tests.test_init_backtest_db — 建表、校验、版本、视图、幂等。"""

from __future__ import annotations

from pathlib import Path

import init_backtest_db as init
import pytest
from quant_schema.constants import BACKTEST_TABLES, BACKTEST_VIEWS


def test_init_and_check(tmp_path: Path) -> None:
    path = tmp_path / "backtest.duckdb"
    with init.database(path) as con:
        init.init_schema(con)
        assert init.installed_version(con) == "1.0.0"
        init.verify_schema(con)
        tables = {
            str(row[0])
            for row in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
            ).fetchall()
        }
        views = {
            str(row[0])
            for row in con.execute(
                "SELECT view_name FROM duckdb_views() WHERE schema_name = 'main'"
            ).fetchall()
        }
        assert set(BACKTEST_TABLES) <= tables
        assert set(BACKTEST_VIEWS) <= views
        counts = init.table_row_counts(con)
        assert counts["bt_baselines"] == 0
        assert counts["meta_schema_version"] == 1

    # 重新打开再 check，验证幂等与持久化
    with init.database(path) as con:
        init.init_schema(con)
        init.verify_schema(con)
        assert init.table_row_counts(con)["bt_nav"] == 0


def test_version_is_backtest_only(tmp_path: Path) -> None:
    path = tmp_path / "backtest.duckdb"
    with init.database(path) as con:
        init.init_schema(con)
        components = {
            row[0]
            for row in con.execute(
                "SELECT component FROM meta_schema_version"
            ).fetchall()
        }
        assert components == {"backtest"}


def test_check_on_empty_db_fails(tmp_path: Path) -> None:
    path = tmp_path / "empty.duckdb"
    with init.database(path) as con, pytest.raises(init.SchemaError, match="缺表"):
        init.verify_schema(con)
