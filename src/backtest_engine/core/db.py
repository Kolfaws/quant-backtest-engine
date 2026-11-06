"""core.db — market / backtest 双库连接。

并发约定（DuckDB 单写多读）：

* ``market.duckdb`` 只以 ``read_only=True`` 打开；factor_research 用它自己的
  RW 连接做 ``DELETE + INSERT`` 增量写，两者可并发；
* ``backtest.duckdb`` 只有本项目的写连接；
* 跨库计算用 ``ATTACH ... (READ_ONLY)``，不要复制数据。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import duckdb

try:  # uv add --editable ../quant-data-pipeline 后可直接导入
    from data_pipeline.db import connect as _duckdb_connect
except ModuleNotFoundError:  # pragma: no cover - 同仓库开发时的兜底
    _candidate = Path(__file__).resolve().parents[4] / "quant-data-pipeline" / "src"
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))
    from data_pipeline.db import connect as _duckdb_connect

from quant_schema.constants import BACKTEST_COMPONENT, BACKTEST_TABLES

from .config import ProfileConfig

MARKET_ALIAS: Final[str] = "market"


class SchemaError(RuntimeError):
    """backtest.duckdb 结构与 quant_schema.backtest_ddl 不一致。"""


def _quote_literal(value: str) -> str:
    return value.replace("'", "''")


def _connect(
    path: Path,
    *,
    read_only: bool,
    threads: int,
    memory_limit: str,
    temp_dir: Path | None,
) -> duckdb.DuckDBPyConnection:
    if read_only and not path.is_file():
        raise FileNotFoundError(f"DuckDB 文件不存在：{path}")
    return _duckdb_connect(
        path,
        read_only=read_only,
        threads=threads,
        memory_limit=memory_limit,
        temp_dir=temp_dir,
    )


def connect_market(profile: ProfileConfig) -> duckdb.DuckDBPyConnection:
    """只读打开 market.duckdb；调用方负责 close()。"""
    if not profile.market_read_only:
        raise SchemaError(
            "backtest_engine 必须以只读方式打开 market.duckdb；"
            "请把 configs/database.yaml 的 market_read_only 设为 true"
        )
    return _connect(
        profile.market_db_path,
        read_only=True,
        threads=profile.threads,
        memory_limit=profile.memory_limit,
        temp_dir=profile.temp_dir,
    )


def connect_backtest(
    profile: ProfileConfig, *, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    """打开 backtest.duckdb；调用方负责 close()。"""
    return _connect(
        profile.backtest_db_path,
        read_only=read_only,
        threads=profile.threads,
        memory_limit=profile.memory_limit,
        temp_dir=profile.temp_dir,
    )


@contextmanager
def market_connection(profile: ProfileConfig) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect_market(profile)
    try:
        yield con
    finally:
        con.close()


@contextmanager
def backtest_connection(
    profile: ProfileConfig, *, read_only: bool = False
) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect_backtest(profile, read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def require_backtest_schema(con: duckdb.DuckDBPyConnection) -> None:
    """写入前的前置检查：backtest 表齐全、版本行存在。"""
    existing = {
        str(row[0])
        for row in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
        ).fetchall()
    }
    missing = [name for name in BACKTEST_TABLES if name not in existing]
    if missing:
        raise SchemaError(
            f"backtest.duckdb 缺表 {missing}；请先执行 "
            "quant-backtest-engine/scripts/init_backtest_db.py init"
        )
    rows = con.execute(
        "SELECT version FROM meta_schema_version WHERE component = ?",
        [BACKTEST_COMPONENT],
    ).fetchall()
    if not rows:
        raise SchemaError(
            "backtest 组件未初始化；请先执行 scripts/init_backtest_db.py init"
        )


def attach_market(con: duckdb.DuckDBPyConnection, profile: ProfileConfig) -> None:
    """把 market.duckdb 以只读方式挂载到当前连接（别名 ``market``）。"""
    attached = {
        str(row[0])
        for row in con.execute(
            "SELECT database_name FROM duckdb_databases()"
        ).fetchall()
    }
    if MARKET_ALIAS in attached:
        return
    if not profile.market_db_path.is_file():
        raise FileNotFoundError(f"market.duckdb 不存在：{profile.market_db_path}")
    con.execute(
        f"ATTACH '{_quote_literal(str(profile.market_db_path))}' "
        f"AS {MARKET_ALIAS} (READ_ONLY)"
    )


def detach_market(con: duckdb.DuckDBPyConnection) -> None:
    attached = {
        str(row[0])
        for row in con.execute(
            "SELECT database_name FROM duckdb_databases()"
        ).fetchall()
    }
    if MARKET_ALIAS in attached:
        con.execute(f"DETACH {MARKET_ALIAS}")


__all__ = [
    "MARKET_ALIAS",
    "SchemaError",
    "attach_market",
    "backtest_connection",
    "connect_backtest",
    "connect_market",
    "detach_market",
    "market_connection",
    "require_backtest_schema",
]
