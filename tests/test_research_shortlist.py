from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.agents.scanner import research_shortlist, scan_universe_from_env
from quant_ai.marketdata.models import Candle

NOW = datetime(2026, 9, 22, 3, tzinfo=timezone.utc)
PREVIOUS = date(2026, 9, 21)
TARGET = date(2026, 9, 22)


class History:
    def __init__(self, *, future=False, missing=False, volume=1000000):
        self.future, self.missing, self.volume = future, missing, volume
        self.clocks = []

    def fetch(self, instrument, now):
        self.clocks.append(now)
        first = datetime(2026, 9, 1, 4, tzinfo=timezone.utc)
        bars = []
        for index in range(20 if self.missing else 21):
            price = Decimal(100 + index)
            bars.append(Candle(instrument, first + timedelta(days=index), price, price + 1,
                               price - 1, price, Decimal(self.volume)))
        if self.future:
            bars.append(Candle(instrument, NOW + timedelta(hours=1), Decimal(1000),
                               Decimal(1001), Decimal(999), Decimal(1000), Decimal(100000000)))
        return tuple(bars)


def universe(count=30):
    return scan_universe_from_env({"PRAMANA_SCAN_UNIVERSE_JSON":
                                  '[' + ','.join(f'"S{i:02d}"' for i in range(count)) + ']'})


def shortlist(history, **kwargs):
    names = universe()
    return research_shortlist(names, history=history, as_of=NOW, target_session=TARGET,
                              previous_session=PREVIOUS,
                              sectors={i.symbol: f"sector{n % 5}" for n, i in enumerate(names)},
                              fixed_watchlist=["S00", "S01"], **kwargs)


def test_bounded_diversified_research_keeps_fixed_comparator_and_execution_closed():
    history = History()
    result = shortlist(history)
    assert len(result["shortlist"]) == 15
    assert len(set(result["shortlist"])) == 15
    assert result["fixed_watchlist"] == ["S00", "S01"]
    assert len(result["universe"]) == 30
    selected = [r for r in result["names"] if r["symbol"] in result["shortlist"]]
    assert all(not r["execution_authorized"] for r in result["names"])
    assert all(r["last_session"] == PREVIOUS.isoformat() for r in selected)
    assert max(sum(r["sector"] == sector for r in selected)
               for sector in {r["sector"] for r in selected}) == 3
    assert history.clocks == [NOW] * 30
    assert all(i.tradable is False for i in universe())


def test_target_day_and_future_bars_cannot_change_prior_session_ranking():
    assert shortlist(History(future=True))["shortlist"] == shortlist(History())["shortlist"]
    assert shortlist(History(future=True))["names"] == shortlist(History())["names"]


@pytest.mark.parametrize("history,reason", [(History(missing=True), "missing_previous_session"),
                                           (History(volume=1), "liquidity_proxy_below_screen")])
def test_missing_or_illiquid_evidence_does_not_fill_quota(history, reason):
    result = shortlist(history)
    assert result["shortlist"] == []
    assert all(reason in r["reasons"] for r in result["names"])


def test_later_availability_is_not_used_and_unknown_spread_is_not_zero():
    result = shortlist(History(), observations={
        "S00": {"published_at": "2026-09-21T10:00:00+00:00",
                "received_at": "2026-09-22T03:01:00+00:00", "catalyst": True,
                "spread_bps": Decimal(2)},
        "S01": {"published_at": "2026-09-21T10:00:00+00:00",
                "received_at": "2026-09-21T10:01:00+00:00", "catalyst": True,
                "spread_bps": Decimal(2)}})
    by_symbol = {r["symbol"]: r for r in result["names"]}
    assert by_symbol["S00"]["catalyst_observed"] is None
    assert by_symbol["S00"]["spread_bps"] is None
    assert by_symbol["S01"]["catalyst_observed"] is True
    assert by_symbol["S01"]["spread_bps"] == "2"
    assert by_symbol["S02"]["spread_bps"] is None


def test_naive_cutoff_and_nonprospective_dates_are_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        research_shortlist(universe(), history=History(), as_of=NOW.replace(tzinfo=None),
                           target_session=TARGET, previous_session=PREVIOUS, sectors={})
    with pytest.raises(ValueError, match="prospective"):
        research_shortlist(universe(), history=History(), as_of=NOW,
                           target_session=PREVIOUS, previous_session=PREVIOUS, sectors={})
