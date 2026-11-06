"""backtest_engine — 回测库（backtest.duckdb）的建表、净值与绩效计算。

职责边界：

* **建表**：``scripts/init_backtest_db.py`` 拥有 ``backtest.duckdb`` 的全部 DDL；
* **写净值**：``engine.nav`` 把策略日收益转换为 ``bt_nav``；
* **算超额**：通过 ``v_bt_nav_excess`` 在 SQL 层关联 ``quant-baseline`` 产出的
  ``bt_baselines``，不在 Python 里搬运基准数据；
* **读行情**：以 ``read_only=True`` 打开 ``market.duckdb``，或用 ``ATTACH ... (READ_ONLY)``
  跨库计算。

本包不建 ``market.duckdb`` 的表，也不写基准数据（那是 data_pipeline 与
quant-baseline 的职责）。
"""

__version__ = "0.1.0"


def main() -> None:  # pragma: no cover - console script 占位
    print("quant-backtest-engine: use scripts/init_backtest_db.py or engine.nav APIs")
