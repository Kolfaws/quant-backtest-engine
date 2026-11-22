#!/usr/bin/env python3
"""promote_test_run.py — 将测试库中的成功 run 发布到正式 backtest 库。

发布范围只有 ``bt_runs / bt_nav / bt_run_metrics`` 三张表。基准相关表
（``bt_benchmarks / bt_baselines / bt_baseline_weights / bt_compute_log``）由
``quant-baseline`` 负责发布，本脚本**不再读写** ``bt_compute_log``。

默认 dry-run：以**只读**方式打开正式库，只做校验与行数预览，不取写锁；
``--apply`` 才在单事务内替换该 run 的三表记录。

发布前置校验（dry-run 与 apply 共用）：

1. test 库的 run 必须为 ``success``；
2. run 的 ``benchmark_id`` 非空，且正式库 ``bt_benchmarks`` 有该定义；
3. 正式库 ``bt_baselines`` 覆盖 run 的每个净值交易日（``v_bt_nav_excess`` 按日 join）；
4. test/prod 两库 backtest schema 版本 == ``BACKTEST_SCHEMA_VERSION``，且三表列名与
   ``quant_schema.constants.BACKTEST_TABLE_COLUMNS`` 一致；
5. test/prod 路径不同、库文件存在。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Final

import duckdb
from quant_schema.constants import (
    BACKTEST_COMPONENT,
    BACKTEST_SCHEMA_VERSION,
    BACKTEST_TABLE_COLUMNS,
    BT_BASELINES,
    BT_BENCHMARKS,
    BT_NAV,
    BT_RUN_METRICS,
    BT_RUNS,
    META_SCHEMA_VERSION,
)

from backtest_engine.core.config import load_profile

#: 发布的三张表（按 run -> nav -> metrics 顺序写入；删除按依赖逆序）。
PROMOTED_TABLES: Final[tuple[str, ...]] = (BT_RUNS, BT_NAV, BT_RUN_METRICS)


class PromotionError(RuntimeError):
    """发布前置条件不满足；消息直接面向运维。"""


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _connect(path: Path, *, read_only: bool) -> duckdb.DuckDBPyConnection:
    """打开正式/测试库。dry-run 必须 ``read_only=True``（不取写锁）。"""
    return duckdb.connect(str(path), read_only=read_only)


def _qualified(alias: str | None, table: str) -> str:
    prefix = f"{alias}." if alias else ""
    return f'{prefix}"{table}"'


def _table_columns(
    con: duckdb.DuckDBPyConnection, alias: str | None, table: str
) -> list[str]:
    try:
        rows = con.execute(f"DESCRIBE {_qualified(alias, table)}").fetchall()
    except duckdb.Error as exc:
        raise PromotionError(f"{alias or '正式库'} 缺少表 {table}：{exc}") from exc
    return [str(row[0]) for row in rows]


def _schema_version(con: duckdb.DuckDBPyConnection, alias: str | None) -> str | None:
    rows = con.execute(
        f"SELECT version FROM {_qualified(alias, META_SCHEMA_VERSION)} "
        "WHERE component = ?",
        [BACKTEST_COMPONENT],
    ).fetchall()
    return None if not rows else str(rows[0][0])


def _require_schema(
    con: duckdb.DuckDBPyConnection, alias: str | None, label: str
) -> None:
    """校验 backtest schema 版本与三表列名，避免 ``SELECT *`` 跨版本错位。"""
    version = _schema_version(con, alias)
    if version is None:
        raise PromotionError(
            f"{label} 未初始化 backtest schema（缺 meta_schema_version 版本行）"
        )
    if version != BACKTEST_SCHEMA_VERSION:
        raise PromotionError(
            f"{label} backtest schema 版本 {version} != 代码期望 "
            f"{BACKTEST_SCHEMA_VERSION}；请先迁移/重建后再发布"
        )
    for table in PROMOTED_TABLES:
        expected = [str(column) for column in BACKTEST_TABLE_COLUMNS[table]]
        actual = _table_columns(con, alias, table)
        missing = [column for column in expected if column not in actual]
        extra = [column for column in actual if column not in expected]
        if missing or extra:
            raise PromotionError(
                f"{label}.{table} 列名与 quant_schema 常量不一致："
                f"缺 {missing}，多 {extra}"
            )


def _scalar(con: duckdb.DuckDBPyConnection, sql: str, params: list[object]) -> Any:
    rows = con.execute(sql, params).fetchall()
    return None if not rows else rows[0][0]


def _load_run(con: duckdb.DuckDBPyConnection, run_id: str) -> tuple[Any, ...] | None:
    rows = con.execute(
        f'SELECT status, benchmark_id, start_date, end_date FROM test_src."{BT_RUNS}" '
        "WHERE run_id = ?",
        [run_id],
    ).fetchall()
    return None if not rows else rows[0]


def _check_benchmark(con: duckdb.DuckDBPyConnection, benchmark_id: object) -> str:
    """benchmark 必须非空且在正式库有定义。"""
    benchmark = "" if benchmark_id is None else str(benchmark_id).strip()
    if not benchmark:
        raise PromotionError(
            "run 的 benchmark_id 为空；无法发布基准相对结果，请先在 test 库用 "
            "--benchmark-id 重跑"
        )
    exists = _scalar(
        con,
        f'SELECT 1 FROM "{BT_BENCHMARKS}" WHERE benchmark_id = ? LIMIT 1',
        [benchmark],
    )
    if not exists:
        raise PromotionError(
            f"正式库缺少 benchmark={benchmark} 定义，请先由 quant-baseline 发布基准"
        )
    return benchmark


def _check_baseline_coverage(
    con: duckdb.DuckDBPyConnection, run_id: str, benchmark: str
) -> tuple[int, Any, Any]:
    """正式库 ``bt_baselines`` 必须覆盖 run 的每个净值交易日。"""
    nav_rows = int(
        _scalar(
            con,
            f'SELECT count(*) FROM test_src."{BT_NAV}" WHERE run_id = ?',
            [run_id],
        )
        or 0
    )
    if nav_rows == 0:
        raise PromotionError(f"run_id={run_id} 没有 bt_nav 净值记录，拒绝发布")
    nav_start, nav_end = con.execute(
        f'SELECT min(trade_date), max(trade_date) FROM test_src."{BT_NAV}" '
        "WHERE run_id = ?",
        [run_id],
    ).fetchall()[0]
    missing = int(
        _scalar(
            con,
            f"""
            SELECT count(*)
            FROM test_src."{BT_NAV}" n
            LEFT JOIN "{BT_BASELINES}" b
              ON b.benchmark_id = ? AND b.trade_date = n.trade_date
            WHERE n.run_id = ? AND b.trade_date IS NULL
            """,
            [benchmark, run_id],
        )
        or 0
    )
    if missing:
        raise PromotionError(
            f"正式库 bt_baselines 未覆盖 run 区间：{missing}/{nav_rows} 个净值交易日"
            f"缺 {benchmark} 基准（run {nav_start}..{nav_end}）；"
            "请先补齐基准再发布"
        )
    return nav_rows, nav_start, nav_end


def _metric_rows(con: duckdb.DuckDBPyConnection, run_id: str) -> int:
    return int(
        _scalar(
            con,
            f'SELECT count(*) FROM test_src."{BT_RUN_METRICS}" WHERE run_id = ?',
            [run_id],
        )
        or 0
    )


def _apply(con: duckdb.DuckDBPyConnection, run_id: str) -> None:
    """单事务替换该 run 的三表记录；不触碰基准四表与 bt_compute_log。"""
    con.execute("BEGIN TRANSACTION")
    try:
        for table in (BT_NAV, BT_RUN_METRICS, BT_RUNS):
            con.execute(f'DELETE FROM "{table}" WHERE run_id = ?', [run_id])
        for table in PROMOTED_TABLES:
            columns = ", ".join(
                f'"{column}"' for column in BACKTEST_TABLE_COLUMNS[table]
            )
            con.execute(
                f'INSERT INTO "{table}" ({columns}) '
                f'SELECT {columns} FROM test_src."{table}" WHERE run_id = ?',
                [run_id],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def _promote(prod_path: Path, test_path: Path, run_id: str, *, apply: bool) -> int:
    # dry-run 只读打开正式库：不取写锁，也不应在只读连接上执行任何写语句。
    prod = _connect(prod_path, read_only=not apply)
    attached = False
    try:
        prod.execute(f"ATTACH '{_quote(str(test_path))}' AS test_src (READ_ONLY)")
        attached = True

        _require_schema(prod, None, "正式库")
        _require_schema(prod, "test_src", "测试库")

        run = _load_run(prod, run_id)
        if run is None:
            raise PromotionError(f"测试库没有 run_id={run_id}")
        status, benchmark_id, _start_date, _end_date = run
        if str(status) != "success":
            raise PromotionError(f"run 状态为 {status}，不是 success")
        benchmark = _check_benchmark(prod, benchmark_id)
        nav_rows, nav_start, nav_end = _check_baseline_coverage(prod, run_id, benchmark)
        metric_rows = _metric_rows(prod, run_id)

        print(
            f"run_id={run_id} success；benchmark={benchmark} "
            f"区间={nav_start}..{nav_end} nav={nav_rows} metrics={metric_rows}"
        )
        if not apply:
            print("dry-run（只读）：未写入。确认后加 --apply")
            return 0

        _apply(prod, run_id)
        print(f"已发布 run_id={run_id}（bt_runs/bt_nav/bt_run_metrics）")
        return 0
    finally:
        if attached:
            try:
                prod.execute("DETACH test_src")
            except duckdb.Error:  # pragma: no cover - 连接已异常关闭
                pass
        prod.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="测试 run -> 正式 backtest 库")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入正式库；默认 dry-run 只读校验",
    )
    args = parser.parse_args(argv)

    try:
        prod_cfg = load_profile(profile="production")
        test_cfg = load_profile(profile="test")
    except Exception as exc:  # noqa: BLE001 - CLI 顶层配置错误
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    test_path = test_cfg.backtest_db_path
    prod_path = prod_cfg.backtest_db_path
    if test_path.resolve() == prod_path.resolve():
        print("测试库与正式库路径相同，拒绝执行", file=sys.stderr)
        return 2
    if not test_path.is_file() or not prod_path.is_file():
        print("测试库或正式库不存在", file=sys.stderr)
        return 2

    try:
        return _promote(prod_path, test_path, args.run_id, apply=args.apply)
    except PromotionError as exc:
        print(f"拒绝发布：{exc}", file=sys.stderr)
        return 1
    except duckdb.Error as exc:
        print(f"DuckDB 错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
