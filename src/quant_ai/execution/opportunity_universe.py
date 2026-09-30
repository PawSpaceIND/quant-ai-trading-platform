"""Paper-only selection inside a fixed authorized catalog; never subscriptions."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from quant_ai.domain.models import AssetClass, Market
from quant_ai.marketdata.tick_integrity import tick_value_issue


class PaperOpportunitySelector:
    def __init__(self, catalog, *, paper_only, snapshot_provider, tick_reader, subscribed_symbols):
        if paper_only is not True:
            raise ValueError("opportunity_paper_only_required")
        self.catalog = tuple(catalog)
        if not self.catalog or len(self.catalog) > 50 or len({i.symbol for i in self.catalog}) != len(self.catalog):
            raise ValueError("opportunity_catalog_scope")
        if any(i.market != Market.INDIA or i.exchange != "NSE" or i.currency != "INR"
               or i.asset_class not in {AssetClass.EQUITY, AssetClass.ETF} or i.is_dated_contract
               or type(i.tradable) is not bool for i in self.catalog):
            raise ValueError("opportunity_catalog_nse_cash_only")
        self.by_symbol = {i.symbol: i for i in self.catalog}
        self.fixed = tuple(i.symbol for i in self.catalog)
        self.snapshot_provider = snapshot_provider
        self.tick_reader = tick_reader
        self.subscribed = frozenset(subscribed_symbols)
        self.pending = {}
        self.entry_symbols = frozenset()
        self.last_evidence = None

    def _fresh_tick(self, symbol, now, staged_at=None):
        try:
            tick = self.tick_reader(symbol)
            if (tick.symbol != symbol or tick.source != "zerodha" or tick_value_issue(tick)
                    or tick.observed_at.utcoffset() is None):
                return False
            age = now - tick.observed_at
            return (timedelta(0) <= age <= timedelta(seconds=60)
                    and (staged_at is None or tick.observed_at > staged_at))
        except (AttributeError, KeyError, ValueError, TypeError, ArithmeticError):
            return False

    def select(self, now: datetime, held_symbols):
        if now.utcoffset() is None:
            raise ValueError("opportunity_aware_clock_required")
        held = frozenset(held_symbols)
        if not held <= self.by_symbol.keys():
            self.entry_symbols = frozenset()
            raise ValueError("opportunity_held_identity_missing")
        reasons, selected, status = {}, None, "dynamic"
        scan_reason = "scan_unavailable_or_error"
        try:
            snapshot = self.snapshot_provider(now)
            stamp = datetime.fromisoformat(snapshot["as_of"])
            names = {r["symbol"]: r for r in snapshot["names"]}
            choices = snapshot["shortlist"]
            if (snapshot["schema"] != "pramana.research_shortlist.v1" or stamp.utcoffset() is None
                    or type(choices) is not list or len(choices) > 15 or len(set(choices)) != len(choices)):
                scan_reason = "scan_invalid"
                raise ValueError("invalid_scan")
            if not timedelta(0) <= now - stamp <= timedelta(hours=24):
                scan_reason = "scan_stale_or_future"
                raise ValueError("stale_scan")
            if snapshot["target_session"] != now.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat():
                scan_reason = "scan_wrong_session"
                raise ValueError("wrong_session")
            if not choices:
                scan_reason = "scan_empty"
                raise ValueError("empty_scan")
            selected = []
            for symbol in choices:
                item = self.by_symbol.get(symbol)
                if (item is None or item.tradable is not True or item.asset_class != AssetClass.EQUITY
                        or names.get(symbol, {}).get("eligible") is not True):
                    reasons[symbol] = "unknown_or_ineligible_catalog_symbol"
                    continue
                selected.append(symbol)
            if not selected:
                scan_reason = "scan_no_eligible_catalog_names"
                raise ValueError("empty_eligible_scan")
        except (AttributeError, KeyError, ValueError, TypeError, ArithmeticError, OSError, RuntimeError):
            status, selected = "fixed_fallback", list(self.fixed)
        active = []
        for symbol in selected:
            item = self.by_symbol[symbol]
            if item.tradable is not True:
                reasons[symbol] = "observation_only"
                continue
            if symbol not in self.subscribed:
                reasons[symbol] = "subscription_not_configured"
                continue
            if status == "dynamic":
                # Selection is never entry permission on the cycle which first observes it.
                staged = self.pending.setdefault(symbol, (now, snapshot["target_session"]))
                if staged[1] != snapshot["target_session"]:
                    staged = self.pending[symbol] = (now, snapshot["target_session"])
                if now <= staged[0] or not self._fresh_tick(symbol, now, staged[0]):
                    reasons[symbol] = "await_later_cycle_observed_subscription_price"
                    continue
            elif not self._fresh_tick(symbol, now):
                reasons[symbol] = "fallback_price_not_fresh"
                continue
            active.append(symbol)
        self.pending = {s: value for s, value in self.pending.items() if status == "dynamic" and s in selected}
        self.entry_symbols = frozenset(active)
        # Holdings stay in the decision universe even if no fresh entry price exists.
        # The full original catalog remains the independent protection universe.
        decisions = set(active) | held
        self.last_evidence = {"schema": "pramana.paper_opportunity_selection.v1",
            "as_of": now.isoformat(), "status": status,
            "scan_reason": "valid_current_session_scan" if status == "dynamic" else scan_reason, "selected": selected,
            "entry_admitted": active, "held_retained": sorted(held), "rejections": reasons,
            "protection_universe": list(self.fixed), "catalog_expansion_authorized": False}
        return tuple(i for i in self.catalog if i.symbol in decisions)

    def entry_issue(self, symbol, now):
        if symbol not in self.entry_symbols or not self._fresh_tick(symbol, now):
            return "paper_opportunity_not_admitted"
        return None


class CatalogResearchSnapshot:
    """Reuse the shortlist engine once per session, inside the existing catalog."""
    def __init__(self, catalog, history, sectors, calendar):
        self.catalog, self.history, self.sectors, self.calendar = catalog, history, sectors, calendar
        self.day, self.snapshot = None, None

    def __call__(self, now):
        from datetime import time

        from quant_ai.agents.scanner import research_shortlist
        from quant_ai.execution.session import MarketState

        zone = ZoneInfo("Asia/Kolkata")
        day = now.astimezone(zone).date()
        if self.calendar.state(Market.INDIA, now, exchange="NSE") != MarketState.REGULAR_HOURS:
            return None
        if self.day == day:
            return self.snapshot
        self.day, self.snapshot = day, None
        previous = day
        for _ in range(30):
            previous -= timedelta(days=1)
            noon = datetime.combine(previous, time(12), zone)
            if self.calendar.state(Market.INDIA, noon, exchange="NSE") == MarketState.REGULAR_HOURS:
                break
        else:
            return None
        self.snapshot = research_shortlist(tuple(i for i in self.catalog
            if i.tradable is True and i.asset_class == AssetClass.EQUITY),
            history=self.history, as_of=now, target_session=day, previous_session=previous,
            sectors=self.sectors, fixed_watchlist=(i.symbol for i in self.catalog))
        return self.snapshot
