from decimal import Decimal

from quant_ai.ai.ensemble import SignalEnsemble
from quant_ai.domain.models import AssetClass, Instrument, Market, Side, Signal


def test_ensemble_builds_positive_ev_opportunity() -> None:
    instrument = Instrument("GLD", Market.USA, AssetClass.ETF, "USD", "NYSEARCA")
    signals = (
        Signal("trend", Decimal("0.8"), Decimal("0.9"), 240),
        Signal("macro", Decimal("0.6"), Decimal("0.8"), 240),
        Signal("regime", Decimal("0.7"), Decimal("0.7"), 240),
    )
    opportunity = SignalEnsemble().score(instrument, signals)
    assert opportunity is not None
    assert opportunity.side is Side.BUY
    assert opportunity.expected_value > 0
    assert opportunity.take_profit_distance > opportunity.stop_distance


def test_ensemble_rejects_low_confidence_signal() -> None:
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    signals = (Signal("weak", Decimal("0.9"), Decimal("0.2"), 30),)
    assert SignalEnsemble().score(instrument, signals) is None
