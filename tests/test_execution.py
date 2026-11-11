"""执行配置、固定滑点与成交约束测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from backtest_engine.core.execution import (
    ConstraintConfig,
    ExecutionConfig,
    ExecutionModel,
    ExecutionRejected,
    FeeConfig,
    MarketSnapshot,
    OrderIntent,
    SlippageConfig,
    load_execution_config,
    register_slippage_policy,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "configs" / "execution.yaml"


def _model(
    *,
    fees: FeeConfig | None = None,
    slippage_bps: float = 5.0,
    constraints: ConstraintConfig | None = None,
) -> ExecutionModel:
    config = ExecutionConfig(
        fees=fees or FeeConfig(),
        slippage=SlippageConfig(mode="fixed", bps=slippage_bps),
        constraints=constraints or ConstraintConfig(),
    )
    return ExecutionModel(config)


def test_default_execution_yaml_loads() -> None:
    config = load_execution_config(CONFIG_PATH)
    assert config.price == "next_vwap"
    assert config.lag_days == 1
    assert config.lot_size == 100
    assert config.fees.commission_bps == pytest.approx(2.0)
    assert config.slippage.mode == "fixed"
    assert config.constraints.max_participation_rate == pytest.approx(0.1)
    assert "commission" in config.summary()


def test_load_override_and_reject_bad_rate(tmp_path: Path) -> None:
    path = tmp_path / "execution.yaml"
    path.write_text(
        """
version: 1
fees:
  commission_bps: 1.5
  min_commission: 3.0
  stamp_tax_bps: 5.0
  transfer_fee_bps: 0.1
slippage:
  mode: fixed
  bps: 2.0
constraints:
  max_participation_rate: 0.2
""",
        encoding="utf-8",
    )
    config = load_execution_config(path)
    assert config.fees.commission_bps == pytest.approx(1.5)
    assert config.fees.stamp_tax_bps == pytest.approx(5.0)
    assert config.constraints.max_participation_rate == pytest.approx(0.2)

    path.write_text(
        "version: 1\nconstraints:\n  max_participation_rate: 1.5\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_execution_config(path)


def test_buy_lot_slippage_min_commission() -> None:
    fill = _model().execute(
        OrderIntent("A", "buy", 150, 10.0),
        MarketSnapshot(volume=10_000),
    )
    assert fill.quantity == 100
    assert fill.fill_price == pytest.approx(10.005)
    assert fill.commission == pytest.approx(5.0)
    assert fill.stamp_tax == 0.0
    assert fill.transfer_fee == 0.0
    assert fill.cash_flow == pytest.approx(-(fill.notional + 5.0))


def test_sell_stamp_tax_and_transfer_fee() -> None:
    fill = _model(
        fees=FeeConfig(
            commission_bps=2.0,
            min_commission=5.0,
            stamp_tax_bps=10.0,
            transfer_fee_bps=1.0,
        ),
        slippage_bps=5.0,
    ).execute(
        OrderIntent("A", "sell", 100, 10.0),
        MarketSnapshot(volume=10_000),
    )
    assert fill.quantity == 100
    assert fill.fill_price == pytest.approx(9.995)
    assert fill.commission == pytest.approx(5.0)
    assert fill.stamp_tax == pytest.approx(0.9995)
    assert fill.transfer_fee == pytest.approx(0.09995)


def test_limit_and_suspension_constraints() -> None:
    model = _model()
    with pytest.raises(ExecutionRejected, match="涨停"):
        model.execute(
            OrderIntent("A", "buy", 100, 10.0),
            MarketSnapshot(is_limit_up=True, volume=10_000),
        )
    with pytest.raises(ExecutionRejected, match="跌停"):
        model.execute(
            OrderIntent("A", "sell", 100, 10.0),
            MarketSnapshot(is_limit_down=True, volume=10_000),
        )
    with pytest.raises(ExecutionRejected, match="停牌"):
        model.execute(
            OrderIntent("A", "buy", 100, 10.0),
            MarketSnapshot(is_suspended=True, volume=10_000),
        )


def test_participation_uses_rounded_quantity() -> None:
    model = _model()
    # 150 股 -> 100 股，<= 1000 * 10% = 100，允许
    fill = model.execute(
        OrderIntent("A", "buy", 150, 10.0),
        MarketSnapshot(volume=1000),
    )
    assert fill.quantity == 100
    with pytest.raises(ExecutionRejected, match="参与量"):
        model.execute(
            OrderIntent("A", "buy", 250, 10.0),
            MarketSnapshot(volume=1000),
        )


def test_dynamic_slippage_registry_hook() -> None:
    @register_slippage_policy("test_constant_20bp")
    def build(config: SlippageConfig):
        class Policy:
            def bps(self, order: OrderIntent) -> float:
                _ = order
                return 20.0

        _ = config
        return Policy()

    config = ExecutionConfig(slippage=SlippageConfig(mode="test_constant_20bp"))
    fill = ExecutionModel(config).execute(
        OrderIntent("A", "buy", 100, 10.0),
        MarketSnapshot(volume=10_000),
    )
    assert fill.fill_price == pytest.approx(10.02)
