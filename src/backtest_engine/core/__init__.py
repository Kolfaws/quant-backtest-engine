"""backtest_engine.core — 配置、连接与异常。"""

from .config import ConfigError, ProfileConfig, load_profile

__all__ = ["ConfigError", "ProfileConfig", "load_profile"]
