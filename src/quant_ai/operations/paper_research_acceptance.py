"""Offline PAPER research qualification, never activation or provider fetching."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from quant_ai.agents.scanner import research_shortlist
from quant_ai.domain.models import AssetClass, Market
from quant_ai.execution.opportunity_universe import PaperOpportunitySelector
from quant_ai.execution.session import MarketCalendar, MarketState, default_holidays
from quant_ai.learning.rolling_evaluation import evaluate_rolling_forecasts
from quant_ai.learning.shadow import _instant
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.timeframes import closed_sessions, venue_for


def cached_research_coverage(catalog, history, *, now, previous_session, calendar=None):
    """Copy audited current-day memory cache only; never warm up/fetch a provider.

    Captured rows are bounded to the selector's 64-bar window. The report describes
    observations, not independent data authenticity or subscription entitlement.
    """
    from quant_ai.marketdata.kite_history import KiteDailyHistoryProvider
    from quant_ai.marketdata.timeframes import DailyHistoryProvider

    catalog = tuple(catalog)
    PaperOpportunitySelector(catalog, paper_only=True, snapshot_provider=None,
                             tick_reader=None, subscribed_symbols=())
    if now.utcoffset() is None or previous_session >= now.astimezone(ZoneInfo("Asia/Kolkata")).date():
        raise ValueError("coverage_prior_session_required")
    calendar = calendar or MarketCalendar(holidays=default_holidays())
    zone = ZoneInfo("Asia/Kolkata")
    expected, day = [], now.astimezone(zone).date()
    for _ in range(90):
        day -= timedelta(days=1)
        if calendar.state(Market.INDIA, datetime.combine(day, time(12), zone), exchange="NSE") == MarketState.REGULAR_HOURS:
            expected.append(day)
            if len(expected) == 21:
                break
    if len(expected) != 21 or previous_session != expected[0]:
        raise ValueError("coverage_exchange_calendar_previous_session")
    supported = type(history) in {DailyHistoryProvider, KiteDailyHistoryProvider}
    rows, captured = [], {}
    for item in catalog:
        if item.tradable is not True or item.asset_class != AssetClass.EQUITY:
            continue
        reasons, bars, closed = [], (), ()
        if not supported:
            reasons.append("unsupported_cached_reader")
        else:
            try:
                bars = tuple(history.cached(item, now)[-64:])
                if any(type(b) is not Candle or b.instrument != item or b.timestamp.utcoffset() is None
                       or any(type(value) is not Decimal or not value.is_finite()
                              for value in (b.open, b.high, b.low, b.close, b.volume)) for b in bars):
                    raise ValueError("invalid_identity_or_timestamp")
                closed = tuple(b for b in closed_sessions(bars, now, venue_for(Market.INDIA))
                               if b.timestamp.astimezone(zone).date() in expected)
                if not bars:
                    reasons.append("current_day_cache_absent")
                if len(closed) < 21:
                    reasons.append("insufficient_prior_sessions")
                if not closed or closed[-1].timestamp.astimezone(ZoneInfo("Asia/Kolkata")).date() != previous_session:
                    reasons.append("missing_previous_session")
            except (AttributeError, TypeError, ValueError, ArithmeticError, OSError, RuntimeError):
                reasons = ["cached_history_invalid_or_error"]
                closed = ()
        if not reasons:
            captured[item.symbol] = closed
        rows.append({"symbol": item.symbol, "captured_bars": len(bars),
                     "prior_closed_sessions": len(closed), "reasons": reasons,
                     "history_qualified": not reasons})
    report = {"schema": "pramana.cached_research_coverage.v1", "as_of": now.isoformat(),
              "previous_session": previous_session.isoformat(), "catalog_count": len(catalog),
              "required_prior_sessions": [d.isoformat() for d in reversed(expected)],
              "eligible_stock_count": len(rows), "qualified_stock_count": len(captured),
              "complete": bool(rows) and len(captured) == len(rows), "names": rows,
              "provider_fetch_performed": False, "source_authenticity_verified": False}
    return report, captured


class _CapturedHistory:
    def __init__(self, bars):
        self.bars = bars

    def fetch(self, instrument, now):
        return self.bars[instrument.symbol]


def qualify_paper_research(catalog, history, *, now, later, previous_session,
                          sectors, subscribed_symbols, ticks, held_symbols=(), paper_only=True, calendar=None):
    """Exercise real selector on two synthetic/captured cycles without a daemon.

    Caller supplies the exchange-calendar previous session and already accepted tick
    objects. This proves deterministic source behavior only, not host readiness.
    """
    if paper_only is not True:
        raise ValueError("opportunity_paper_only_required")
    if later.utcoffset() is None or not timedelta(0) < later - now <= timedelta(minutes=30):
        raise ValueError("qualification_later_cycle_required")
    if later.astimezone(ZoneInfo("Asia/Kolkata")).date() != now.astimezone(ZoneInfo("Asia/Kolkata")).date():
        raise ValueError("qualification_same_session_required")
    catalog = tuple(catalog)
    coverage, bars = cached_research_coverage(catalog, history, now=now,
                                            previous_session=previous_session, calendar=calendar)
    snapshot = None
    if coverage["complete"]:
        snapshot = research_shortlist(tuple(i for i in catalog if i.symbol in bars),
            history=_CapturedHistory(bars), as_of=now,
            target_session=now.astimezone(ZoneInfo("Asia/Kolkata")).date(),
            previous_session=previous_session, sectors=sectors,
            fixed_watchlist=tuple(i.symbol for i in catalog))
    reader = dict(ticks)
    selector = PaperOpportunitySelector(catalog, paper_only=True, snapshot_provider=lambda at: snapshot,
        tick_reader=reader.get, subscribed_symbols=subscribed_symbols)
    selector.select(now, held_symbols)
    first = selector.last_evidence
    selector.select(later, held_symbols)
    second = selector.last_evidence
    return {"schema": "pramana.paper_research_qualification.v1", "coverage": coverage,
            "research": snapshot, "first_cycle": first, "later_cycle": second,
            "supported_catalog": list(selector.fixed),
            "selected_names": second["selected"], "entry_admitted": second["entry_admitted"],
            "fixed_comparator": list(selector.fixed), "mode": "OFFLINE_PAPER_QUALIFICATION",
            "runtime_readiness_verified": False, "activation_authorized": False,
            "trading_authorized": False, "risk_policy_changed": False}


def evaluate_frozen_paper_research(package_raw, *, source_grants, folds, frozen_at,
                                   run_id, candidate_id, clock=None):
    """Require a declared pre-holdout plan, then reuse the after-cost scorer.

    frozen_at is a caller declaration, not authenticated prospective evidence.
    Every fold is scored; no fold/model selection or runtime promotion occurs.
    """
    frozen_at = _instant(frozen_at)
    if (type(folds) is not list or not folds
            or any(type(fold) is not dict or "holdout_start" not in fold
                   or frozen_at >= _instant(fold["holdout_start"]) for fold in folds)):
        raise ValueError("research_plan_must_precede_all_holdouts")
    result = evaluate_rolling_forecasts(package_raw, source_grants=source_grants, folds=folds,
        run_id=run_id, candidate_id=candidate_id, clock=clock)
    criteria = {name: all(Decimal(comparison[key]) > 0
                         for key in ("brier_improvement", "log_loss_improvement"))
                for name, comparison in result["comparisons"].items()}
    return {"schema": "pramana.frozen_paper_research_acceptance.v1", "frozen_at": frozen_at.isoformat(),
            "evaluation": result, "pooled_better_than_baseline": criteria,
            "declared_plan_authenticity_verified": False, "activation_authorized": False,
            "portfolio_returns_evaluated": False, "fixed_vs_dynamic_strategy_evaluated": False,
            "limitations": ["Retrospective forecast comparison, not realized strategy returns.",
                            "A caller-declared freeze is not proof of prospective registration.",
                            "Passing these checks cannot authorize trading or establish live readiness."]}
