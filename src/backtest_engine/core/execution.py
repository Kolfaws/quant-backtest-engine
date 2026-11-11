"""交易成本、滑点与成交约束配置 / 固定值执行模型。

当前只实现 YAML 固定值：

* 手续费：佣金（含最低佣金）、印花税（仅卖出）、过户费；
* 滑点：固定 bps，买入上浮、卖出下调；
* 成交约束：涨跌停、停牌、ST、最大参与率、整手。

扩展方式：``slippage.mode`` / ``constraints.mode`` 走注册表。未来新增
``volume_impact`` 等动态函数时，只需注册一个新的 builder，不需要改
``ExecutionModel`` 与 YAML 结构。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from .config import PROJECT_ROOT, ConfigError

Side = Literal["buy", "sell"]
PriceMode = Literal["next_open", "next_close", "next_vwap"]


@dataclass(frozen=True, slots=True)
class FeeConfig:
    """单边/双边费用参数；bps = 万分之一。"""

    commission_bps: float = 2.0
    min_commission: float = 5.0
    stamp_tax_bps: float = 10.0  # 仅卖出
    transfer_fee_bps: float = 0.0


@dataclass(frozen=True, slots=True)
class SlippageConfig:
    """滑点策略配置；目前 ``fixed``，未来可加 ``params`` 给动态实现。"""

    mode: str = "fixed"
    bps: float = 5.0
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConstraintConfig:
    """成交约束配置；目前 ``fixed``，未来可注册动态检查器。"""

    mode: str = "fixed"
    allow_limit_up_buy: bool = False
    allow_limit_down_sell: bool = False
    allow_suspension_trade: bool = False
    allow_st_trade: bool = True
    max_participation_rate: float = 0.1
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """一份完整的执行配置。"""

    version: int = 1
    price: PriceMode = "next_vwap"
    lag_days: int = 1
    lot_size: int = 100
    fees: FeeConfig = field(default_factory=FeeConfig)
    slippage: SlippageConfig = field(default_factory=SlippageConfig)
    constraints: ConstraintConfig = field(default_factory=ConstraintConfig)

    def summary(self) -> str:
        """写入 bt_runs.cost_model 的紧凑审计摘要。"""
        return (
            f"fees=commission:{self.fees.commission_bps}bp/"
            f"min:{self.fees.min_commission}/"
            f"stamp:{self.fees.stamp_tax_bps}bp/"
            f"transfer:{self.fees.transfer_fee_bps}bp;"
            f"slip={self.slippage.mode}:{self.slippage.bps}bp;"
            f"constraints={self.constraints.mode};"
            f"price={self.price};lag={self.lag_days};lot={self.lot_size}"
        )


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """一笔待执行的订单；价格由调用方按 signal/执行时点选好。"""

    ts_code: str
    side: Side
    quantity: int
    reference_price: float


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """执行日的可交易状态；volume 单位为股/份。"""

    is_limit_up: bool = False
    is_limit_down: bool = False
    is_suspended: bool = False
    is_st: bool = False
    volume: float | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    """成交结果与成本分解。"""

    ts_code: str
    side: Side
    quantity: int
    reference_price: float
    fill_price: float
    notional: float
    commission: float
    stamp_tax: float
    transfer_fee: float
    slippage_cost: float

    @property
    def total_fee(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee

    @property
    def cash_flow(self) -> float:
        """买入为负现金流，卖出为正现金流（含费用）。"""
        if self.side == "buy":
            return -(self.notional + self.total_fee)
        return self.notional - self.total_fee


class ExecutionRejected(RuntimeError):
    """订单被成交约束拒绝。"""


class SlippagePolicy(Protocol):
    """滑点策略协议；返回单边 bps。"""

    def bps(self, order: OrderIntent) -> float: ...


class ConstraintPolicy(Protocol):
    """成交约束策略协议。"""

    def check(self, order: OrderIntent, market: MarketSnapshot) -> None: ...


class FixedSlippagePolicy:
    """固定 bps 滑点。"""

    def __init__(self, bps: float) -> None:
        self._bps = float(bps)

    def bps(self, order: OrderIntent) -> float:
        _ = order
        return self._bps


class FixedConstraintPolicy:
    """固定规则：涨跌停/停牌/ST/参与率。"""

    def __init__(self, config: ConstraintConfig) -> None:
        self.config = config

    def check(self, order: OrderIntent, market: MarketSnapshot) -> None:
        if order.quantity <= 0:
            raise ExecutionRejected("quantity 必须 > 0")
        if market.is_suspended and not self.config.allow_suspension_trade:
            raise ExecutionRejected(f"{order.ts_code} 停牌，不能成交")
        if market.is_st and not self.config.allow_st_trade:
            raise ExecutionRejected(f"{order.ts_code} 为 ST，按配置禁止成交")
        if (
            order.side == "buy"
            and market.is_limit_up
            and not self.config.allow_limit_up_buy
        ):
            raise ExecutionRejected(f"{order.ts_code} 涨停，不能买入")
        if (
            order.side == "sell"
            and market.is_limit_down
            and not self.config.allow_limit_down_sell
        ):
            raise ExecutionRejected(f"{order.ts_code} 跌停，不能卖出")
        rate = self.config.max_participation_rate
        if rate > 0:
            if market.volume is None:
                raise ExecutionRejected(f"{order.ts_code} 缺少成交量，无法校验参与率")
            max_qty = market.volume * rate
            if order.quantity > max_qty:
                raise ExecutionRejected(
                    f"{order.ts_code} 委托 {order.quantity} 超过最大参与量 {max_qty:.2f}"
                )


SLIPPAGE_REGISTRY: dict[str, Callable[[SlippageConfig], SlippagePolicy]] = {
    "fixed": lambda config: FixedSlippagePolicy(config.bps),
}
CONSTRAINT_REGISTRY: dict[str, Callable[[ConstraintConfig], ConstraintPolicy]] = {
    "fixed": lambda config: FixedConstraintPolicy(config),
}


def register_slippage_policy(
    name: str,
) -> Callable[
    [Callable[[SlippageConfig], SlippagePolicy]],
    Callable[[SlippageConfig], SlippagePolicy],
]:
    """注册动态滑点策略；只在插件初始化时调用。"""

    def decorator(func):
        if name in SLIPPAGE_REGISTRY:
            raise ValueError(f"滑点策略已注册：{name}")
        SLIPPAGE_REGISTRY[name] = func
        return func

    return decorator


def register_constraint_policy(
    name: str,
) -> Callable[
    [Callable[[ConstraintConfig], ConstraintPolicy]],
    Callable[[ConstraintConfig], ConstraintPolicy],
]:
    """注册动态成交约束策略；只在插件初始化时调用。"""

    def decorator(func):
        if name in CONSTRAINT_REGISTRY:
            raise ValueError(f"约束策略已注册：{name}")
        CONSTRAINT_REGISTRY[name] = func
        return func

    return decorator


class ExecutionModel:
    """把订单转成 Fill：约束 -> 整手 -> 滑点 -> 费用。"""

    def __init__(
        self,
        config: ExecutionConfig,
        *,
        slippage_policy: SlippagePolicy | None = None,
        constraint_policy: ConstraintPolicy | None = None,
    ) -> None:
        self.config = config
        self.slippage = slippage_policy or _build_slippage(config.slippage)
        self.constraints = constraint_policy or _build_constraints(config.constraints)

    def execute(self, order: OrderIntent, market: MarketSnapshot) -> Fill:
        if order.reference_price <= 0:
            raise ValueError("reference_price 必须 > 0")
        lot = self.config.lot_size
        quantity = (order.quantity // lot) * lot
        if quantity <= 0:
            raise ExecutionRejected(
                f"{order.ts_code} 委托 {order.quantity} 不足一手（lot_size={lot}）"
            )
        # 约束检查使用整手后的数量，避免 150 股订单被 120 股参与量误拒。
        rounded = replace(order, quantity=quantity)
        self.constraints.check(rounded, market)
        signed_bps = self.slippage.bps(rounded)
        direction = 1.0 if order.side == "buy" else -1.0
        fill_price = order.reference_price * (1.0 + direction * signed_bps / 10_000.0)
        notional = fill_price * quantity
        fees = self.config.fees
        commission = max(notional * fees.commission_bps / 10_000.0, fees.min_commission)
        stamp_tax = (
            notional * fees.stamp_tax_bps / 10_000.0 if order.side == "sell" else 0.0
        )
        transfer_fee = notional * fees.transfer_fee_bps / 10_000.0
        return Fill(
            ts_code=order.ts_code,
            side=order.side,
            quantity=quantity,
            reference_price=order.reference_price,
            fill_price=fill_price,
            notional=notional,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            slippage_cost=abs(fill_price - order.reference_price) * quantity,
        )


def _build_slippage(config: SlippageConfig) -> SlippagePolicy:
    try:
        builder = SLIPPAGE_REGISTRY[config.mode]
    except KeyError as exc:
        raise ConfigError(
            f"未知滑点策略 {config.mode!r}；可选 {sorted(SLIPPAGE_REGISTRY)}"
        ) from exc
    return builder(config)


def _build_constraints(config: ConstraintConfig) -> ConstraintPolicy:
    try:
        builder = CONSTRAINT_REGISTRY[config.mode]
    except KeyError as exc:
        raise ConfigError(
            f"未知成交约束策略 {config.mode!r}；可选 {sorted(CONSTRAINT_REGISTRY)}"
        ) from exc
    return builder(config)


def _require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} 必须是 mapping")
    return value


def _reject_unknown(
    block: Mapping[str, Any], allowed: set[str], field_name: str
) -> None:
    unknown = set(block) - allowed
    if unknown:
        raise ConfigError(f"{field_name} 含未知字段：{sorted(unknown)}")


def _float(
    block: Mapping[str, Any], key: str, default: float, field_name: str
) -> float:
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field_name}.{key} 必须是数字")
    if value < 0:
        raise ConfigError(f"{field_name}.{key} 不能为负数")
    return float(value)


def _bool(block: Mapping[str, Any], key: str, default: bool, field_name: str) -> bool:
    value = block.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name}.{key} 必须是 bool")
    return value


def load_execution_config(path: str | Path | None = None) -> ExecutionConfig:
    """加载 ``execution.yaml``；未知字段、负费率、非法枚举立即报错。"""
    config_path = (
        Path(path) if path is not None else PROJECT_ROOT / "configs" / "execution.yaml"
    )
    if not config_path.is_file():
        raise ConfigError(f"执行配置不存在：{config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data = _require_mapping(raw, "execution.yaml")
    _reject_unknown(
        data,
        {"version", "execution", "fees", "slippage", "constraints"},
        "execution.yaml",
    )
    if data.get("version") != 1:
        raise ConfigError("execution.yaml.version 必须为 1")

    execution = _require_mapping(data.get("execution", {}), "execution")
    _reject_unknown(execution, {"price", "lag_days", "lot_size"}, "execution")
    price = str(execution.get("price", "next_vwap"))
    if price not in {"next_open", "next_close", "next_vwap"}:
        raise ConfigError(f"execution.price 非法：{price!r}")
    lag_days = execution.get("lag_days", 1)
    lot_size = execution.get("lot_size", 100)
    if not isinstance(lag_days, int) or lag_days < 1:
        raise ConfigError("execution.lag_days 必须是 >=1 的整数")
    if not isinstance(lot_size, int) or lot_size < 1:
        raise ConfigError("execution.lot_size 必须是 >=1 的整数")

    fee_block = _require_mapping(data.get("fees", {}), "fees")
    _reject_unknown(
        fee_block,
        {"commission_bps", "min_commission", "stamp_tax_bps", "transfer_fee_bps"},
        "fees",
    )
    fees = FeeConfig(
        commission_bps=_float(fee_block, "commission_bps", 2.0, "fees"),
        min_commission=_float(fee_block, "min_commission", 5.0, "fees"),
        stamp_tax_bps=_float(fee_block, "stamp_tax_bps", 10.0, "fees"),
        transfer_fee_bps=_float(fee_block, "transfer_fee_bps", 0.0, "fees"),
    )

    slip_block = _require_mapping(data.get("slippage", {}), "slippage")
    _reject_unknown(slip_block, {"mode", "bps", "params"}, "slippage")
    slippage = SlippageConfig(
        mode=str(slip_block.get("mode", "fixed")),
        bps=_float(slip_block, "bps", 5.0, "slippage"),
        params=_require_mapping(slip_block.get("params", {}), "slippage.params"),
    )

    cons_block = _require_mapping(data.get("constraints", {}), "constraints")
    allowed_constraints = {
        "mode",
        "allow_limit_up_buy",
        "allow_limit_down_sell",
        "allow_suspension_trade",
        "allow_st_trade",
        "max_participation_rate",
        "params",
    }
    _reject_unknown(cons_block, allowed_constraints, "constraints")
    rate = _float(cons_block, "max_participation_rate", 0.1, "constraints")
    if rate > 1.0:
        raise ConfigError("constraints.max_participation_rate 必须 <= 1.0")
    constraints = ConstraintConfig(
        mode=str(cons_block.get("mode", "fixed")),
        allow_limit_up_buy=_bool(
            cons_block, "allow_limit_up_buy", False, "constraints"
        ),
        allow_limit_down_sell=_bool(
            cons_block, "allow_limit_down_sell", False, "constraints"
        ),
        allow_suspension_trade=_bool(
            cons_block, "allow_suspension_trade", False, "constraints"
        ),
        allow_st_trade=_bool(cons_block, "allow_st_trade", True, "constraints"),
        max_participation_rate=rate,
        params=_require_mapping(cons_block.get("params", {}), "constraints.params"),
    )
    return ExecutionConfig(
        price=price,  # type: ignore[arg-type]
        lag_days=lag_days,
        lot_size=lot_size,
        fees=fees,
        slippage=slippage,
        constraints=constraints,
    )


__all__ = [
    "CONSTRAINT_REGISTRY",
    "SLIPPAGE_REGISTRY",
    "ConstraintConfig",
    "ConstraintPolicy",
    "ExecutionConfig",
    "ExecutionModel",
    "ExecutionRejected",
    "FeeConfig",
    "Fill",
    "FixedConstraintPolicy",
    "FixedSlippagePolicy",
    "MarketSnapshot",
    "OrderIntent",
    "SlippageConfig",
    "SlippagePolicy",
    "load_execution_config",
    "register_constraint_policy",
    "register_slippage_policy",
]
