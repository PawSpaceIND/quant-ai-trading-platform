"""Declared strategy families for research and paper qualification.

These descriptors make supported hypotheses visible to Atlas and the dashboard.
They are metadata, not a permission to place live orders or a claim of
profitability.
"""
from __future__ import annotations

STRATEGY_CATALOG = (
    {
        "id": "momentum.v1",
        "label": "Time-series momentum",
        "products": ("equity", "etf", "future", "fx", "commodity"),
        "sides": ("LONG", "SHORT_RESEARCH"),
        "horizon": "swing",
        "requiredEvidence": ("closed daily bars", "cost/slippage", "forward paper run"),
        "status": "paper_only",
    },
    {
        "id": "mean_reversion",
        "label": "Mean reversion",
        "products": ("equity", "etf", "future", "fx"),
        "sides": ("LONG", "SHORT_RESEARCH"),
        "horizon": "short swing",
        "requiredEvidence": ("stationarity/threshold review", "cost/slippage", "forward paper run"),
        "status": "paper_only",
    },
    {
        "id": "breakout",
        "label": "Volatility breakout",
        "products": ("equity", "future", "commodity", "fx"),
        "sides": ("LONG", "SHORT_RESEARCH"),
        "horizon": "intraday to swing",
        "requiredEvidence": ("session-aware bars", "gap/slippage stress", "forward paper run"),
        "status": "paper_only",
    },
    {
        "id": "futures-trend-roll",
        "label": "Futures trend with roll",
        "products": ("future",),
        "sides": ("LONG", "SHORT_RESEARCH"),
        "horizon": "contract lifecycle",
        "requiredEvidence": ("exact contract/expiry", "roll calendar", "margin and settlement", "forward paper run"),
        "status": "research_only",
    },
    {
        "id": "defined-risk-option-spread",
        "label": "Defined-risk option spread",
        "products": ("option",),
        "sides": ("DEBIT_SPREAD", "CREDIT_SPREAD_RESEARCH"),
        "horizon": "expiry-aware",
        "requiredEvidence": ("exact legs/strikes", "Greeks/IV source", "margin and assignment", "forward paper run"),
        "status": "research_only",
    },
    {
        "id": "covered-call-protective-put",
        "label": "Covered call / protective put",
        "products": ("equity", "option"),
        "sides": ("COVERED", "PROTECTIVE"),
        "horizon": "portfolio hedge",
        "requiredEvidence": ("underlying/leg linkage", "corporate actions", "assignment/settlement", "forward paper run"),
        "status": "research_only",
    },
    {
        "id": "pairs-relative-value",
        "label": "Pairs and relative value",
        "products": ("equity", "future", "etf"),
        "sides": ("LONG_SHORT_RESEARCH",),
        "horizon": "market-neutral research",
        "requiredEvidence": ("pair linkage", "borrow/shortability", "leg synchronisation", "forward paper run"),
        "status": "research_only",
    },
    {
        "id": "commodity-calendar-spread",
        "label": "Commodity calendar spread",
        "products": ("commodity", "metal", "future"),
        "sides": ("LONG_SHORT_RESEARCH",),
        "horizon": "contract spread",
        "requiredEvidence": ("two exact expiries", "delivery/quality rules", "margin offsets", "forward paper run"),
        "status": "research_only",
    },
)
