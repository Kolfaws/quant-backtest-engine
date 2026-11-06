#!/usr/bin/env python3
"""init_backtest_db.py — ``backtest.duckdb`` 结构初始化（backtest 组件）。

与 ``data_pipeline/scripts/init_market_db.py`` 相同的工程模式：

1. 表/视图定义来自 ``quant_schema.backtest_ddl``（本脚本不硬编码 DDL）；
2. 建表、校验、建视图、写版本在**同一个事务**里，失败整体回滚；
3. 结构漂移（缺表 / 缺列 / 类型不符 / 多余列）直接 ``SchemaError``；
4. ``meta_schema_version`` 独立于 market 库，``component='backtest'``。

用法：
    python scripts/init_backtest_db.py init  --db "$BACKTEST_DB_PATH"
    python scripts/init_backtest_db.py check --db "$BACKTEST_DB_PATH"
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import duckdb
from quant_schema._ddl import Table, normalise_type
from quant_schema.backtest_ddl import BACKTEST_TABLES, BACKTEST_VIEWS
from quant_schema.constants import BACKTEST_COMPONENT, BACKTEST_SCHEMA_VERSION


def _load_dotenv() -> None:
    """读取项目根目录 .env；不覆盖已有环境变量。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

LOG = logging.getLogger("init_backtest_db")
SCHEMA_VERSION: Final[str] = BACKTEST_SCHEMA_VERSION
COMPONENT: Final[str] = BACKTEST_COMPONENT
TABLES: Final[tuple[Table, ...]] = BACKTEST_TABLES
VIEWS: Final[dict[str, str]] = BACKTEST_VIEWS

DEFAULT_DB_PATH: Final[Path] = Path(
    os.getenv("BACKTEST_DB_PATH", "data/backtest.duckdb")
)
TEST_DB_PATH: Final[Path | None] = (
    Path(os.environ["BACKTEST_TEST_DB_PATH"])
    if os.getenv("BACKTEST_TEST_DB_PATH")
    else None
)
DEFAULT_THREADS: Final[int] = 6
DEFAULT_MEMORY_LIMIT: Final[str] = "6GB"

_MEMORY_LIMIT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d+(?:\.\d+)?(?:MB|GB|TB)$", re.IGNORECASE
)
_COMPONENT_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ConfigError(ValueError):
    """参数非法。"""


class SchemaError(RuntimeError):
    """库结构与代码期望不一致。"""


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def _validate_threads(threads: int) -> int:
    if not isinstance(threads, int) or isinstance(threads, bool):
        raise ConfigError(f"threads 必须是 int，收到 {type(threads).__name__}")
    if not 1 <= threads <= 128:
        raise ConfigError(f"threads 必须在 [1, 128]，收到 {threads}")
    return threads


def _validate_memory_limit(memory_limit: str) -> str:
    if not isinstance(memory_limit, str) or not _MEMORY_LIMIT_RE.match(
        memory_limit.strip()
    ):
        raise ConfigError(f"memory_limit 非法：{memory_limit!r}，示例 '4GB'")
    return memory_limit.strip()


def _validate_component(component: str) -> str:
    if not isinstance(component, str) or not _COMPONENT_RE.fullmatch(component):
        raise ConfigError(f"component 非法：{component!r}，示例 'backtest'")
    return component


def _quote_literal(value: str) -> str:
    return value.replace("'", "''")


def _chmod_quiet(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError as exc:  # pragma: no cover - Windows 常见
        LOG.debug("chmod %s 失败：%s", path, exc)


def connect(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    threads: int = DEFAULT_THREADS,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    temp_dir: str | Path | None = None,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """打开（并按需创建）DuckDB 连接；调用方负责 close()。"""
    _validate_threads(threads)
    _validate_memory_limit(memory_limit)

    is_memory = str(db_path) == ":memory:"
    target = str(db_path)
    if not is_memory:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = str(path)

    con = duckdb.connect(target, read_only=read_only)
    try:
        con.execute(f"SET threads = {_validate_threads(threads)}")
        con.execute(f"SET memory_limit = '{_validate_memory_limit(memory_limit)}'")
        con.execute("SET preserve_insertion_order = false")
        con.execute("SET enable_progress_bar = false")
        if temp_dir is not None:
            tmp = Path(temp_dir)
            tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
            con.execute(f"SET temp_directory = '{_quote_literal(str(tmp.resolve()))}'")
    except Exception:
        con.close()
        raise

    if not is_memory and not read_only:
        _chmod_quiet(Path(db_path), 0o600)
    return con


@contextmanager
def database(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    threads: int = DEFAULT_THREADS,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    temp_dir: str | Path | None = None,
    read_only: bool = False,
) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect(
        db_path,
        threads=threads,
        memory_limit=memory_limit,
        temp_dir=temp_dir,
        read_only=read_only,
    )
    try:
        yield con
    finally:
        try:
            con.close()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("关闭连接异常：%s", exc)


# ---------------------------------------------------------------------------
# 校验 / 建表
# ---------------------------------------------------------------------------
def _scalar(
    con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[object] = ()
) -> Any:
    cursor = con.execute(sql, list(params)) if params else con.execute(sql)
    rows = cursor.fetchall()
    if not rows:
        raise SchemaError(f"查询未返回任何行：{sql}")
    return rows[0][0]


def verify_schema(con: duckdb.DuckDBPyConnection) -> None:
    """比对实际表结构与 quant_schema.backtest_ddl.BACKTEST_TABLES。"""
    rows = con.execute(
        "SELECT table_name, column_name, data_type "
        "FROM duckdb_columns() WHERE schema_name = 'main'"
    ).fetchall()
    actual: dict[str, dict[str, str]] = {}
    for table_name, column_name, data_type in rows:
        actual.setdefault(str(table_name), {})[str(column_name)] = normalise_type(
            str(data_type)
        )

    problems: list[str] = []
    for table in TABLES:
        found = actual.get(table.name)
        if found is None:
            problems.append(f"缺表 {table.name}")
            continue
        expected = table.expected_types
        for column, want in expected.items():
            got = found.get(column)
            if got is None:
                problems.append(f"{table.name}.{column} 缺失")
            elif got != want:
                problems.append(f"{table.name}.{column} 类型 {got} != {want}")
        extra = sorted(set(found) - set(expected))
        if extra:
            problems.append(f"{table.name} 存在多余列 {extra}")

    existing_views = {
        str(row[0])
        for row in con.execute(
            "SELECT view_name FROM duckdb_views() WHERE schema_name = 'main'"
        ).fetchall()
    }
    missing_views = [name for name in VIEWS if name not in existing_views]
    if missing_views:
        problems.append(f"缺视图 {missing_views}")

    if problems:
        raise SchemaError(
            "schema 校验失败（backtest，请勿直接修改建表语句，需走迁移）："
            + "; ".join(problems)
        )


def init_schema(con: duckdb.DuckDBPyConnection, *, verify: bool = True) -> None:
    """幂等建表 + 建视图 + 记录版本号；DDL 与校验同事务。"""
    con.execute("BEGIN TRANSACTION")
    try:
        for table in TABLES:
            con.execute(table.ddl())

        for name, sql in VIEWS.items():
            con.execute(f'CREATE OR REPLACE VIEW "{name}" AS {sql}')

        if verify:
            verify_schema(con)

        con.execute(
            "INSERT INTO meta_schema_version (component, version) VALUES (?, ?) "
            "ON CONFLICT (component) DO UPDATE SET "
            "version = excluded.version, applied_at = now()",
            [COMPONENT, SCHEMA_VERSION],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def installed_version(
    con: duckdb.DuckDBPyConnection, component: str = COMPONENT
) -> str | None:
    """返回指定组件版本；默认 'backtest'。"""
    component = _validate_component(component)
    rows = con.execute(
        "SELECT version FROM meta_schema_version WHERE component = ?", [component]
    ).fetchall()
    return None if not rows else str(rows[0][0])


def table_row_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    existing = {
        str(row[0])
        for row in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
        ).fetchall()
    }
    out: dict[str, int] = {}
    for table in TABLES:
        if table.name not in existing:
            out[table.name] = -1
            continue
        out[table.name] = int(_scalar(con, f'SELECT count(*) FROM "{table.name}"'))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="init_backtest_db.py",
        description="backtest.duckdb 结构初始化（backtest 组件）",
    )
    parser.add_argument("command", nargs="?", default="init", choices=("init", "check"))
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--env", choices=("prod", "test"), default="prod")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    parser.add_argument(
        "--memory-limit", dest="memory_limit", default=DEFAULT_MEMORY_LIMIT
    )
    parser.add_argument("--temp-dir", dest="temp_dir", type=Path, default=None)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def _setup_logging(level: str) -> None:
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s | %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.db is not None:
        db_path = args.db
    elif args.env == "test":
        if TEST_DB_PATH is None:
            LOG.error("未配置 BACKTEST_TEST_DB_PATH")
            return 2
        db_path = TEST_DB_PATH
    else:
        db_path = DEFAULT_DB_PATH
    _setup_logging(args.log_level)
    try:
        with database(
            db_path,
            threads=args.threads,
            memory_limit=args.memory_limit,
            temp_dir=args.temp_dir,
        ) as con:
            if args.command == "check":
                verify_schema(con)
            else:
                init_schema(con)
            counts = table_row_counts(con)
            LOG.info(
                "%s 完成  db=%s  component=%s  version=%s",
                "校验" if args.command == "check" else "初始化",
                db_path,
                COMPONENT,
                installed_version(con) or "-",
            )
            for name, rows in counts.items():
                LOG.info("  %-24s %10d", name, rows)
    except ConfigError as exc:
        LOG.error("配置错误：%s", exc)
        return 2
    except duckdb.Error as exc:
        LOG.error("DuckDB 错误：%s", exc)
        return 1
    except SchemaError as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
