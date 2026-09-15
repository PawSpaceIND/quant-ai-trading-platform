"""Source-labelled daily closes for exploratory cash-portfolio risk analytics."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Market
from quant_ai.execution.session import GlobalVenue, MarketCalendar, MarketState, default_holidays


def risk_history_input(rows: list[dict], captured_at: datetime, *, calendar: MarketCalendar | None = None) -> dict:
    """Exclude the current local date, even after close; never invent missing bars.

    The bundled regular-session calendar is maintained for 2026 only. Other years
    explicitly withhold the model until that calendar is qualified and extended.
    Provider adjustments and instrument mapping remain unverified declarations.
    """
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    local = captured_at.astimezone(ZoneInfo("Asia/Kolkata"))
    if local.year != 2026:
        return {"status": "unavailable", "reason": "NSE risk-history calendar needs qualification for this year."}
    calendar = calendar or MarketCalendar(default_holidays())
    # The bounded 2026 support and documented special-session set remain unchanged.
    # Only explicit closure additions may narrow the bundled sessions for this input.
    baseline = MarketCalendar(default_holidays())
    allowed_special = baseline.special_sessions.get(GlobalVenue.INDIA, frozenset())
    if calendar.special_sessions.get(GlobalVenue.INDIA, frozenset()) != allowed_special:
        return {"status": "unavailable", "reason": "NSE special-session configuration needs qualification for risk history."}
    holidays = calendar.holidays.get(GlobalVenue.INDIA, frozenset())
    if not holidays.issuperset(baseline.holidays[GlobalVenue.INDIA]):
        return {"status": "unavailable", "reason": "NSE risk-history calendar cannot remove bundled closures without qualification."}
    start = date(2026, 1, 1)
    sessions = []
    day = start
    while day < local.date():
        if calendar.state(Market.INDIA, datetime.combine(day, time(12), local.tzinfo)) == MarketState.REGULAR_HOURS:
            sessions.append(day.isoformat())
        day += timedelta(days=1)
    instruments = []
    for row in rows:
        identity = row.get("instrument")
        if not identity:
            continue
        # Risk history uses the bundled NSE cash-session calendar. Keep
        # unlabelled legacy fixtures, but exclude explicitly identified BSE
        # rows and all derivatives, commodities, funds and IFSC rows rather
        # than assigning them an unqualified equity calendar.
        if (
            identity.get("market") is not None and identity.get("market") != "INDIA"
        ) or (
            identity.get("exchange") is not None
            and identity.get("exchange") not in {"NSE"}
        ) or (
            identity.get("assetClass") is not None
            and identity.get("assetClass") not in {"EQUITY", "ETF", "INDEX"}
        ):
            continue
        # An unavailable quote has no historical evidence. Keep it visible in
        # the market snapshot, but do not create an empty risk instrument that
        # could be mistaken for a zero-return series.
        if not row.get("history"):
            continue
        observations = []
        for bar in row.get("history", []):
            # Preserve invalid records for the consumer to reject; do not filter
            # negative prices, duplicates or gaps into a cleaner-looking sample.
            raw_date = str(bar.get("date", ""))
            try:
                parsed = datetime.fromisoformat(raw_date)
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(local.tzinfo)
                session_date = parsed.date()
            except ValueError:
                observations.append({"date": raw_date, "close": bar.get("close")})
                continue
            if start <= session_date < local.date():
                observations.append({"date": session_date.isoformat(), "close": bar.get("close")})
        instruments.append({**identity, "observations": observations})
    result = {
        "schema": "pramana.risk_history.v1", "asOf": captured_at.isoformat(),
        "source": "Zerodha historical day candles",
        "priceBasis": "provider_close_adjustments_unverified",
        "calendar": {"name": "NSE cash sessions with documented Budget Sunday", "timezone": "Asia/Kolkata",
                     "coverageStart": "2026-01-01", "coverageEnd": "2026-12-31",
                     "version": "bundled_nse_2026_with_cmtr72349", "sessions": sessions,
                     "specialSessions": sorted(d.isoformat() for d in calendar.special_sessions[GlobalVenue.INDIA]),
                     "specialSessionSource": "https://nsearchives.nseindia.com/content/circulars/CMTR72349.pdf"},
        "instruments": instruments,
    }
    extra_closures = sorted(d.isoformat() for d in holidays - baseline.holidays[GlobalVenue.INDIA])
    if extra_closures:
        result["calendar"].update(
            name="NSE cash sessions with configured closure additions",
            version="bundled_nse_2026_with_cmtr72349_and_configured_closures",
            configuredClosures=extra_closures,
            configuredClosuresSha256=hashlib.sha256(json.dumps(extra_closures, separators=(",", ":")).encode()).hexdigest(),
            configuredClosureQualification="operator_supplied_unverified",
        )
    return result
