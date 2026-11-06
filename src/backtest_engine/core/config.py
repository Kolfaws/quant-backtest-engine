"""core.config — backtest_engine 连接配置。

只读取 ``configs/database.yaml``：market 库路径、backtest 库路径、资源上限。
不做任何建表或连接动作。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_ENV_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)


class ConfigError(ValueError):
    """配置缺失或非法。"""


def _load_dotenv(path: Path | None = None) -> None:
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def expand_env(value: str) -> str:
    """展开 ``${VAR}`` / ``${VAR:-默认值}``。"""

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ and os.environ[name] != "":
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(f"环境变量 {name} 未定义，且没有默认值")

    return _ENV_PATTERN.sub(replace, value)


def _resolve_path(raw: str) -> Path:
    path = Path(expand_env(raw)).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    name: str
    market_db_path: Path
    backtest_db_path: Path
    market_read_only: bool
    threads: int
    memory_limit: str
    temp_dir: Path | None


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} 必须是 mapping，收到 {type(value).__name__}")
    return value


def load_profile(
    config_dir: Path | None = None, profile: str | None = None
) -> ProfileConfig:
    """加载并校验连接配置。"""
    _load_dotenv()
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("缺少 pyyaml：请执行 uv add pyyaml") from exc

    directory = config_dir or (PROJECT_ROOT / "configs")
    path = directory / "database.yaml"
    if not path.is_file():
        raise ConfigError(f"配置文件不存在：{path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} YAML 解析失败：{exc}") from exc

    data = _require_mapping(raw, "database.yaml")
    if data.get("version") != 1:
        raise ConfigError("database.yaml.version 必须为 1")

    profiles = _require_mapping(data.get("profiles"), "profiles")
    name = (
        profile or os.environ.get("BACKTEST_DB_PROFILE") or data.get("active_profile")
    )
    if not name:
        raise ConfigError("未指定 active_profile / BACKTEST_DB_PROFILE")
    if name not in profiles:
        raise ConfigError(f"profile {name!r} 不存在；可选 {sorted(profiles)}")

    block = _require_mapping(profiles[name], f"profiles.{name}")
    required = ("market_db_path", "backtest_db_path")
    missing = [key for key in required if key not in block]
    if missing:
        raise ConfigError(f"profiles.{name} 缺字段 {missing}")

    threads = block.get("threads", 6)
    if (
        not isinstance(threads, int)
        or isinstance(threads, bool)
        or not 1 <= threads <= 128
    ):
        raise ConfigError(f"profiles.{name}.threads 非法：{threads!r}")

    memory_limit = str(block.get("memory_limit", "6GB"))
    if not re.fullmatch(r"^\d+(?:\.\d+)?(?:MB|GB|TB)$", memory_limit, re.IGNORECASE):
        raise ConfigError(f"profiles.{name}.memory_limit 非法：{memory_limit!r}")

    temp_dir = block.get("temp_dir")
    return ProfileConfig(
        name=str(name),
        market_db_path=_resolve_path(str(block["market_db_path"])),
        backtest_db_path=_resolve_path(str(block["backtest_db_path"])),
        market_read_only=bool(block.get("market_read_only", True)),
        threads=threads,
        memory_limit=memory_limit,
        temp_dir=_resolve_path(str(temp_dir)) if temp_dir else None,
    )


__all__ = ["PROJECT_ROOT", "ConfigError", "ProfileConfig", "expand_env", "load_profile"]
