"""backtest_engine.core — 配置、执行成本与异常。"""

from .config import ConfigError, ProfileConfig, load_profile
from .execution import (
    ConstraintConfig,
    ExecutionConfig,
    ExecutionModel,
    ExecutionRejected,
    FeeConfig,
    Fill,
    MarketSnapshot,
    OrderIntent,
    SlippageConfig,
    load_execution_config,
    register_constraint_policy,
    register_slippage_policy,
)

__all__ = [
    "ConfigError",
    "ConstraintConfig",
    "ExecutionConfig",
    "ExecutionModel",
    "ExecutionRejected",
    "FeeConfig",
    "Fill",
    "MarketSnapshot",
    "OrderIntent",
    "ProfileConfig",
    "SlippageConfig",
    "load_execution_config",
    "load_profile",
    "register_constraint_policy",
    "register_slippage_policy",
]
