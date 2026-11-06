"""backtest_engine.engine — 净值、绩效与基准对比。"""

from .nav import RunSpec, compute_metrics, mark_run_failed, register_run, write_nav

__all__ = [
    "RunSpec",
    "compute_metrics",
    "mark_run_failed",
    "register_run",
    "write_nav",
]
