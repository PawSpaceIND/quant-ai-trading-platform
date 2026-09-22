"""Whether the forecast is calibrated - scored only over rows proven to share one mapping.

A Brier score describes one mapping from evidence to probability. Until #235 the LLM
overlay rewrote the forecast from the model's own stance and confidence, under the SAME
basis id as the deterministic mapping, on every decision where the model answered. The
paper host ran that code until 22 September 2026. So a row labelled
``weighted_lean_times_confidence.v1`` may have come from either mapping, and a score
computed over all of them describes neither: it is the calibration of a blend that no
code path ever ran.

The label cannot settle that, because the label is exactly what the overlay kept. This
recomputes instead. The journal now carries the two inputs the v1 mapping reads - the
weighted lean and the average conviction - so each stored probability is tested against
the value its own inputs produce:

    verified      inputs recorded, and the mapping reproduces the stored probability
    by_path       no inputs, but the row came from a mode only the deterministic
                  mapping ever wrote to, so there was one producer
    contaminated  inputs recorded, and the mapping does NOT reproduce the probability:
                  a second mapping under the same id - the basis is mixed
    unverifiable  no inputs, from a mode the overlay could have rewritten
    unregistered  a basis id this build has no mapping for

Only verified and by_path rows are scored. A contaminated row fails the report outright;
an unverifiable one is excluded and counted, never blended in and never assumed clean.

Nothing here decides, sizes or gates. It reports, and it never arms anything - arming the
EV gate stays an operator act that needs this report to pass AND the payoffs to be
measured rather than declared.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from quant_ai.agents import forecast
from quant_ai.analytics import empirical_payoffs
from quant_ai.analytics.decision_journal import parse_decimal
from quant_ai.analytics.forecast_scoring import summarize
from quant_ai.analytics.promotion import promotion_report

SCHEMA = "pramana.calibration_report.v1"
# One step of the mapping's own output. Recomputing from the journal's four-place inputs
# moves the probability by at most exactly this - measured over 200,000 draws - so a
# tighter tolerance would call honest rounding a second mapping.
QUANTUM = Decimal("0.0001")
# Every basis this build can reproduce. A refit ships a new id AND registers its mapping
# here; a row under an id with no entry is reported, never scored against a stand-in.
MAPPINGS = {forecast.BASIS: forecast.probability_up}
# Modes on which only the deterministic mapping ever wrote a forecast. The pre-#235
# overlay wrote on every path where the model returned a parsable signal; an invalid
# schema returned the deterministic hold untouched.
SINGLE_PRODUCER_MODES = frozenset({"deterministic", "llm_invalid_schema"})

VERIFIED, BY_PATH = "verified", "by_path"
CONTAMINATED, UNVERIFIABLE, UNREGISTERED = "contaminated", "unverifiable", "unregistered"
CLEAN = frozenset({VERIFIED, BY_PATH})


def integrity_of(row: Any) -> str | None:
    """Which of the five a row is, or None when it carries no forecast to check."""
    if not isinstance(row, dict):
        return None
    probability = parse_decimal(row.get("forecast_probability_up"))
    basis = row.get("forecast_basis")
    if probability is None or not basis:
        return None
    mapping = MAPPINGS.get(str(basis))
    if mapping is None:
        return UNREGISTERED
    lean = parse_decimal(row.get("consensus_weighted_score"))
    conviction = parse_decimal(row.get("consensus_confidence"))
    if lean is not None and conviction is not None:
        return VERIFIED if abs(mapping(lean, conviction) - probability) <= QUANTUM else CONTAMINATED
    return BY_PATH if row.get("mode") in SINGLE_PRODUCER_MODES else UNVERIFIABLE


def basis_integrity(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per basis id, how many rows are provably its own - and whether any provably are not."""
    tally: dict[str, dict[str, Any]] = {}
    for row in rows:
        state = integrity_of(row)
        if state is None:
            continue
        counts = tally.setdefault(str(row.get("forecast_basis")), {
            VERIFIED: 0, BY_PATH: 0, CONTAMINATED: 0, UNVERIFIABLE: 0, UNREGISTERED: 0,
        })
        counts[state] += 1
    for counts in tally.values():
        # Mixed is proven, not suspected: at least one row whose own inputs do not produce
        # its own probability. Unverifiable rows are a gap in the record, not a proof.
        counts["mixed"] = counts[CONTAMINATED] > 0
    return dict(sorted(tally.items()))


def live_drawdown_limit(directives: Any = None) -> Decimal:
    """The drawdown limit the warden actually applies: the tighter of the plan and 0.10.

    The same rule as ``RiskWarden`` and the daemon's own breaker - ``min(plan,
    RiskPolicy().max_drawdown)`` - rebuilt from the directives the container reads, so
    the promotion report is measured against the limit the book really ran under. The
    0.10 baseline alone is the loosest limit the book can have, and comparing a realised
    drawdown against it could pass a book that had already breached its real one.
    """
    from quant_ai.governance.directives import FounderDirectives
    from quant_ai.planning.capital import CapitalGoalEngine
    from quant_ai.risk.policy import RiskPolicy

    found = directives if directives is not None else FounderDirectives()
    plan = CapitalGoalEngine().recommend(found.capital_plan_request())
    return min(plan.max_drawdown_fraction, RiskPolicy().max_drawdown)


def realised_max_drawdown(connection: Any, tenant_id: str) -> Decimal | None:
    """Worst peak-to-trough fall of the book's recorded daily equity, or None.

    None when the ledger holds no equity mark for the tenant. The promotion report then
    names the gap and refuses, rather than treating a book with no history as a book that
    never fell. One session is already a curve - the table keeps that day's opening and
    its last mark - the same rule derive_strategy_evidence applies.
    """
    from quant_ai.analytics.metrics import maximum_drawdown
    from quant_ai.governance.derived_evidence import daily_equity_curve

    curve = daily_equity_curve(connection, tenant_id)
    return maximum_drawdown(curve) if len(curve) >= 2 else None


def calibrate(
    rows: Sequence[dict[str, Any]],
    *,
    realised_max_drawdown: Any = None,
    policy_max_drawdown: Any = None,
) -> dict[str, Any]:
    """The whole report: integrity first, then everything else over the clean rows only."""
    rows = list(rows)
    integrity = basis_integrity(rows)
    clean = [row for row in rows if integrity_of(row) in CLEAN]
    promotion = promotion_report(
        clean, realised_max_drawdown=realised_max_drawdown,
        policy_max_drawdown=policy_max_drawdown,
    )
    contaminated = sum(counts[CONTAMINATED] for counts in integrity.values())
    # A mixed basis fails the report whatever the clean subset scores. Dropping the
    # contaminated rows and passing the rest would certify a basis id that provably meant
    # two things, and the next reader would have no way to know.
    verdict = "basis_mixed" if contaminated else promotion["verdict"]
    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "promotion_authorized": verdict == "pass",
        "integrity": integrity,
        "excluded": {
            state: sum(counts[state] for counts in integrity.values())
            for state in (CONTAMINATED, UNVERIFIABLE, UNREGISTERED)
        },
        "scored_rows": len(clean),
        # Both baselines, because the coin is the easy one: a forecaster that has only
        # learned how often the move happens beats it while knowing nothing specific.
        "scoring": summarize(clean),
        "promotion": promotion,
        # Payoffs measure what the market did after each decision, net of that row's own
        # cost - which does not depend on which mapping produced the probability, so every
        # resolved row counts here. See quant_ai.analytics.empirical_payoffs.
        "payoffs": empirical_payoffs.artifact(rows),
        "limitations": [
            "Scored only over rows whose probability is reproduced by one registered mapping.",
            "Unverifiable rows are excluded, not assumed clean; they predate the recorded inputs.",
            "The drawdown limit is rebuilt from the directives this process reads, as the warden does.",
            "This report arms nothing. The EV gate also needs measured, not declared, payoffs.",
        ],
    }


# The promotion report's missing-input codes, in the words an operator needs. One of them
# predates #238: drift has been journaled since then, so that code now fires only when no
# filled row exists to carry a drift - which is what the operator should be told.
MISSING_GLOSS = {
    "no_scored_basis": "no clean forecast has resolved yet",
    "brier_or_baseline_unavailable": "no clean forecast has resolved yet",
    "no_settled_filled_buy": "no filled buy has settled - the book has not traded",
    "entry_drift_not_journaled": "no filled row carries drift_bps yet - the book has not traded",
    "drawdown_or_policy_unavailable": "fewer than two daily equity marks, or no drawdown limit",
}


def _shown(value: Any) -> str:
    return "-" if value is None else str(value)


def _line(label: str, value: Any) -> str:
    return f"  {label:<32} {_shown(value)}"


def render(report: dict[str, Any]) -> str:
    """Terminal text for the operator. The verdict first, then why."""
    promotion = report["promotion"]
    lines = [
        (f"Forecast calibration  verdict={report['verdict']}  "
         f"promotion_authorized={str(report['promotion_authorized']).lower()}"),
        "",
        "Basis integrity (a probability is scored only if its own inputs reproduce it)",
    ]
    if not report["integrity"]:
        lines.append("  no forecasts journaled")
    for basis, counts in report["integrity"].items():
        lines.append(
            f"  {basis}: verified={counts[VERIFIED]} by_path={counts[BY_PATH]} "
            f"unverifiable={counts[UNVERIFIABLE]} contaminated={counts[CONTAMINATED]} "
            f"unregistered={counts[UNREGISTERED]}{'  MIXED' if counts['mixed'] else ''}"
        )
    lines += ["", "Scored sample"]
    lines.append(_line("resolved forecasts (clean)", promotion["resolved_forecast_count"]))
    lines.append(_line("minimum required", promotion["minimum_resolved"]))
    lines.append(_line("Brier", promotion["brier_score"]))
    lines.append(_line("Brier, coin (p=0.5)", promotion["brier_baseline_coin_flip"]))
    champion = next((item for item in report["scoring"]["by_basis"]
                     if item["basis"] == promotion["basis"]), None)
    base_rate = (champion or {}).get("baselines") or {}
    lines.append(_line("Brier, base rate", (base_rate.get("base_rate") or {}).get("brier_score")))
    lines.append(_line("skill vs base rate", promotion["skill_vs_base_rate"]))
    lines += ["", "Book"]
    lines.append(_line("filled buys (probes count)", promotion["filled_buys"]))
    lines.append(_line("mean net P&L, filled buys", promotion["mean_net_ev_filled_buys"]))
    lines.append(_line("median |drift_bps|, fills", promotion["median_abs_entry_drift_bps"]))
    lines.append(_line("realised max drawdown", promotion["realised_max_drawdown"]))
    lines.append(_line("drawdown limit (live)", promotion["policy_max_drawdown"]))
    overall = report["payoffs"]["overall"]
    lines += ["", "Payoffs (measured, net of each row's own cost; declared are 1% / 2%)"]
    lines.append(_line("e_win_hat / e_loss_hat",
                       f"{_shown(overall['e_win_hat'])} / {_shown(overall['e_loss_hat'])}"))
    lines.append(_line("break-even p (measured)", overall["break_even_probability"]))
    lines.append(_line("break-even p (declared)", "0.7000"))
    lines.append(_line("sample", f"{overall['n']}{' (thin)' if overall['thin'] else ''}"))
    if promotion["missing_inputs"]:
        lines += ["", "Missing inputs (the report refuses until these exist):"]
        lines += [f"  {code}: {MISSING_GLOSS.get(code, code)}" for code in promotion["missing_inputs"]]
    excluded = report["excluded"]
    if any(excluded.values()):
        lines += ["", "Excluded from scoring: " + ", ".join(
            f"{state}={count}" for state, count in excluded.items() if count)]
    return "\n".join(lines)
