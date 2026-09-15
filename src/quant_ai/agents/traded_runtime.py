"""One assembly of the runtime that trades, shared by the daemon and the replay.

:class:`SwarmPaperTradingService` fills every unsupplied argument with a permissive
default: no position cap, no blocked asset class, an unarmed book firewall, an Atlas with
no founder instructions. That is right for a unit test and wrong for a backtest, because
a curve produced by a looser configuration than the one that trades is not evidence about
the engine - it is evidence about an engine nobody runs. Both callers assemble here so
the two cannot drift apart again, and ``runtime_configuration`` gives a test something
comparable to hold them to.
"""

from __future__ import annotations

import logging

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.swarm import AtlasCIOAgent
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.analytics.attribution import restore_from_journal
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.governance.directives import FounderDirectives
from quant_ai.governance.runtime_manifest import describe
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.risk.book_history import sector_map_from_env
from quant_ai.risk.overnight import OvernightExposureFirewall, overnight_risk_from_env
from quant_ai.risk.policy import BookRiskFirewall
from quant_ai.risk.warden import RiskWarden

LOGGER = logging.getLogger("quant_ai.traded_runtime")

# Which agent turns evidence into an action. The live runtime asks the model when a
# client is configured and falls back to the rule-based consensus otherwise; a replay can
# only honestly run the rule-based one. See ``quant_ai.backtesting.replay``.
DETERMINISTIC_CONSENSUS = "deterministic_consensus"
LLM_CONSENSUS = "llm_consensus"

# What a result is and is not evidence of, keyed by the decision-maker that produced it.
# It travels on the replay run evidence and on the tearsheet so that nobody has to read
# the code to read a number honestly.
DECISION_MAKER_NOTES = {
    DETERMINISTIC_CONSENSUS: (
        "Produced by the deterministic Atlas consensus - the rule-based stand-in the live "
        "runtime falls back to when no model client is configured, the provider is down or "
        "the daily budget is spent. It is NOT a backtest of the LLM decision-maker that "
        "normally trades, and is no evidence about that decision-maker's skill."
    ),
    LLM_CONSENSUS: (
        "Produced by the LLM consensus. A model may have been trained on data covering "
        "this window, which is look-ahead no assertion in this engine can detect, so the "
        "result is not evidence of out-of-sample skill."
    ),
}

# Attributes of the service that say where a decision is written rather than what may be
# decided. Everything else is policy and must appear in ``runtime_configuration``.
PER_RUN_ATTRIBUTES = frozenset({"broker", "xai_logger"})

# The policy attributes ``runtime_configuration`` accounts for. A new knob on the service
# fails ``tests/test_backtest_fidelity.py`` until it is fingerprinted here too, which is
# what stops a future control from being live-only by accident.
GOVERNED_ATTRIBUTES = frozenset(
    {
        "cio",
        "warden",
        "attribution",
        "stress_agent",
        "kill_switch",
        "allow_position_scaling",
        "max_open_positions",
        "snapshot_provider",
        "pre_submit_check",
        "strategy_manifest_provider",
    }
)


def build_traded_runtime(
    *,
    broker: PaperBrokerService,
    directives: FounderDirectives | None = None,
    llm_client: AnthropicSwarmClient | None = None,
    xai_logger: XAITraceLogger | None = None,
    book_risk_history=None,
    overnight_risk: OvernightExposureFirewall | None = None,
    attribution_journal_tenant: str | None = None,
) -> SwarmPaperTradingService:
    """The sanctioned execution runtime under one set of founder directives.

    ``book_risk_history`` arms the correlation and expected-shortfall limits and is left
    unset by callers that have no return history, because once armed an unusable
    measurement blocks every entry by design. ``overnight_risk`` bounds what may be
    carried through a close; it is read from the environment here when the caller has no
    calendar of its own, so the daemon and the replay can never arm it differently.
    ``attribution_journal_tenant`` rebuilds the specialist scores from that tenant's
    closed trades; a caller that cannot bound those outcomes to its own window must leave
    it unset rather than import a future.
    """
    directives = directives or FounderDirectives()
    runtime = SwarmPaperTradingService(
        cio=AtlasCIOAgent(
            AtlasInvestmentAgent(
                llm_client=llm_client, founder_instructions=directives.instructions
            )
        ),
        warden=RiskWarden(
            blocked_asset_classes=directives.blocked_asset_classes(),
            book_risk=BookRiskFirewall(
                history_provider=book_risk_history,
                sector_map=directives.sector_map or sector_map_from_env(),
            ),
            overnight_risk=overnight_risk or overnight_risk_from_env(),
        ),
        broker=broker,
        xai_logger=xai_logger,
        max_open_positions=directives.max_open_positions,
    )
    if attribution_journal_tenant is not None:
        try:
            restore_from_journal(
                runtime.attribution, broker, tenant_id=attribution_journal_tenant
            )
        except Exception:  # a cold start beats a runtime that will not boot
            LOGGER.exception(
                "attribution_restore_failed tenant=%s", attribution_journal_tenant
            )
    return runtime


def decision_maker_of(runtime: SwarmPaperTradingService) -> str:
    """Which consensus this runtime would run: the model when it has a client, else rules.

    It reports the intent, not the outcome of one tick: the live path still falls back to
    the rule-based consensus when the provider is down, the daily budget is spent or a
    specialist veto puts the decision on a hard hold.
    """
    return LLM_CONSENSUS if runtime.cio.atlas.llm_client is not None else DETERMINISTIC_CONSENSUS


def runtime_configuration(runtime: SwarmPaperTradingService) -> dict:
    """Every knob that decides what this runtime may trade, as comparable data.

    Two runtimes with equal configurations answer the same proposal the same way. That is
    the property a backtest needs from the thing it claims to be testing.
    """
    issues: list[str] = []
    components = {
        name: describe(item, issues)
        for name, item in {
            "runtime": runtime,
            "cio": runtime.cio,
            "atlas": runtime.cio.atlas,
            "llm": runtime.cio.atlas.llm_client,
            "warden": runtime.warden,
            "attribution": runtime.attribution,
            "stress": runtime.stress_agent,
        }.items()
    }
    return {
        "components": components,
        "componentIssues": issues,
        "decisionMaker": decision_maker_of(runtime),
        # A cold engine weights every specialist at 1.0; a restored one does not. The
        # count is what separates the two without exporting the scores themselves.
        "attributionObservations": sum(
            item.observations for item in runtime.attribution.attribution()
        ),
        "killSwitchEngaged": runtime.kill_switch.engaged,
        "hooks": {
            "snapshotProvider": runtime.snapshot_provider is not None,
            "preSubmitCheck": runtime.pre_submit_check is not None,
            "strategyManifestProvider": runtime.strategy_manifest_provider is not None,
        },
    }


def policy_attributes(runtime: SwarmPaperTradingService) -> frozenset[str]:
    """Public attributes of the service that decide what it may trade."""
    return frozenset(name for name in vars(runtime) if not name.startswith("_")) - PER_RUN_ATTRIBUTES
