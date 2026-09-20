"""Read-only preview of the production environment's intelligence provider selection.

Constructs the same provider graph as the environment factory. It never fetches a
provider or starts a runner. Configuration is not source truth or live readiness.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from quant_ai.intelligence.external.fred import FredMacroProvider
from quant_ai.intelligence.external.india_macro import (
    CompositeMacroProvider,
    NseInstitutionalFlowsProvider,
    YahooIndiaVixProvider,
)
from quant_ai.intelligence.external.rss import RssNewsSentimentAdapter
from quant_ai.intelligence.external.yahoo_fundamentals import YahooFundamentalsProvider
from quant_ai.intelligence.failover import (
    FailoverFundamentalProvider,
    FailoverMacroProvider,
    FailoverNewsProvider,
    ProviderCategory,
    ProviderFailoverRegistry,
)


def _require(condition, reason):
    if not condition:
        raise ValueError("intelligence_inputs_" + reason)


# The parts the macro composite may carry, by exact type, and the label each reports as.
MACRO_PART_LABELS = {
    FredMacroProvider: "fred",
    YahooIndiaVixProvider: "yahoo_india_vix",
    NseInstitutionalFlowsProvider: "nse_fii_dii",
}


def inspect_intelligence_configuration(*, clock=None):
    """Inspect this process's environment; no API call or runtime readiness approval."""
    _require(os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false") == "false", "paper_only")
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    _require(isinstance(now, datetime) and now.tzinfo is not None
             and now.utcoffset() is not None, "aware_clock")
    # Lazy import avoids booting the daemon or introducing another selection policy.
    from quant_ai.daemon import _env_intelligence_providers

    selected = _env_intelligence_providers()
    _require(type(selected) is tuple and len(selected) == 3, "provider_graph")
    rules = (
        ("news", ProviderCategory.NEWS, FailoverNewsProvider, RssNewsSentimentAdapter, "rss"),
        ("fundamentals", ProviderCategory.FUNDAMENTALS, FailoverFundamentalProvider,
         YahooFundamentalsProvider, "yahoo_fundamentals"),
        ("macro", ProviderCategory.MACRO, FailoverMacroProvider, CompositeMacroProvider, "composite"),
    )
    inputs = {}
    registry = None
    for wrapper, (name, category, wrapper_type, adapter_type, label) in zip(selected, rules):
        _require(type(wrapper) is wrapper_type
                 and type(wrapper.registry) is ProviderFailoverRegistry, "unexpected_wrapper")
        _require(registry is None or wrapper.registry is registry, "registry_binding")
        registry = wrapper.registry
        registered = registry._providers.get(category, ())
        _require(type(registered) is tuple and len(registered) <= 1, "adapter_count")
        _require(all(type(adapter) is adapter_type for adapter in registered), "unexpected_adapter")
        inputs[name] = {
            "selection": "configured_unverified" if registered else "not_configured",
            "adapter": label if registered else None,
            "data_availability": "not_probed",
            "freshness": "not_probed",
        }
        if registered and adapter_type is CompositeMacroProvider:
            parts = registered[0].parts
            _require(type(parts) is tuple and len(parts) >= 1, "macro_parts")
            _require(all(type(part) in MACRO_PART_LABELS for part in parts), "unexpected_macro_part")
            _require(len({type(part) for part in parts}) == len(parts), "duplicate_macro_part")
            # Only the guard above keeps an unknown type out; the label lookup itself never
            # raises, so removing that guard is a wrong report, not a crash.
            inputs[name]["parts"] = [MACRO_PART_LABELS.get(type(part), "unsupported") for part in parts]
    _require(set(registry._providers) <= {rule[1] for rule in rules}, "unexpected_category")
    return {
        "schema": "pramana.intelligence_configuration.v1",
        "mode": "CONFIGURATION_ONLY",
        "checked_at": now.astimezone(timezone.utc).isoformat(),
        "scope": "current_process_environment_factory_preview",
        "inputs": inputs,
        "provider_fetch_performed": False,
        "running_process_verified": False,
        "source_authenticity_verified": False,
        "trading_authorized": False,
        "limitations": [
            "Selection is not proof of valid credentials, successful retrieval or fresh data.",
            "This separate process cannot attest which objects the running engine selected.",
            "Price feeds, model responses, token validity, alerts and restore are not inspected.",
            "Source licensing, instrument meaning, data vintages and costs need separate review.",
        ],
    }
