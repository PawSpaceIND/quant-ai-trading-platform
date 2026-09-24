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
from statistics import median
from typing import Any

from quant_ai.agents import forecast
from quant_ai.agents.expected_value import expected_value
from quant_ai.analytics import empirical_payoffs
from quant_ai.analytics.decision_journal import parse_decimal
from quant_ai.analytics.forecast_scoring import BASIS_POINT, MINIMUM_SCORED, summarize
from quant_ai.analytics.promotion import FILLED, promotion_report, row_expected_value

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


def _mean(values: Sequence[Decimal]) -> str | None:
    return str((sum(values, Decimal(0)) / Decimal(len(values))).quantize(Decimal("0.000001"))) if values else None


def _rupees(values: Sequence[Decimal]) -> str | None:
    return str((sum(values, Decimal(0)) / Decimal(len(values))).quantize(Decimal("0.01"))) if values else None


def _negative_share(values: Sequence[Decimal]) -> str | None:
    if not values:
        return None
    return str((Decimal(sum(1 for value in values if value < 0)) / Decimal(len(values))).quantize(Decimal("0.0001")))


def empirical_expected_value(row: dict[str, Any], measured: Any) -> Decimal | None:
    """The row's own probability and cost, priced at the measured payoffs that apply to it.

    None when no measured group applies - including a thin one, exactly as
    ``empirical_payoffs.group_for`` refuses it. Reported beside the declared EV and read
    by nothing: the live expected value, and anything that sizes, keeps the declared
    numbers.
    """
    group = empirical_payoffs.group_for(
        measured, symbol=row.get("symbol"), playbook=row.get("playbook"), regime=row.get("regime"),
    )
    probability = parse_decimal(row.get("forecast_probability_up"))
    cost_bps = parse_decimal(row.get("forecast_cost_bps"))
    if group is None or probability is None or cost_bps is None:
        return None
    win, loss = parse_decimal(group.get("e_win_hat")), parse_decimal(group.get("e_loss_hat"))
    if win is None or loss is None:
        return None
    return expected_value(probability, expected_return=win, expected_risk=loss, cost_bps=cost_bps)


def declared_break_even(rows: Sequence[dict[str, Any]]) -> str | None:
    """Median over rows of (risk + cost) / (win + risk) at each row's OWN declared payoffs.

    Derived rather than printed as a constant: the 0.70 the specialists' 1% / 2% imply is a
    property of those numbers, and a specialist that declares differently changes it.
    """
    values = []
    for row in rows:
        win, risk = parse_decimal(row.get("expected_return")), parse_decimal(row.get("expected_risk"))
        cost = parse_decimal(row.get("forecast_cost_bps"))
        if win is None or risk is None or cost is None or win + risk <= 0:
            continue
        values.append((risk + cost / BASIS_POINT) / (win + risk))
    return str(median(values).quantize(Decimal("0.0001"))) if values else None


def _ev_block(rows: Sequence[dict[str, Any]], measured: Any) -> dict[str, Any]:
    declared = [value for row in rows if (value := row_expected_value(row)) is not None]
    empirical = [value for row in rows if (value := empirical_expected_value(row, measured)) is not None]
    return {
        "declared": {"n": len(declared), "mean": _mean(declared), "negative_share": _negative_share(declared)},
        "empirical": {"n": len(empirical), "mean": _mean(empirical), "negative_share": _negative_share(empirical)},
    }


def is_probe(row: dict[str, Any]) -> bool:
    """A labelled probe: the journal's own flag, never inferred from size or confidence."""
    return str(row.get("probe")) == "1"


def entered(rows: Sequence[dict[str, Any]], measured: Any) -> dict[str, dict[str, Any]]:
    """Filled buys, as conviction entries and labelled probes apart and together.

    Only decisions that became positions: a hold or a refusal has an expected value but no
    fill, and averaging its EV in would describe trades the book never took. Probes are
    told apart because they clear a lower lean by design (PRAMANA_EXPLORATION_MIN_WEIGHTED_SCORE);
    pooled, their weaker signal would be read as the conviction entries' result.
    Realised P&L is in rupees per fill; expected value is a return on the notional.
    """
    filled = [row for row in rows
              if row.get("governance") == FILLED and str(row.get("side") or "").upper() == "BUY"]
    groups = {
        "all": filled,
        "conviction": [row for row in filled if not is_probe(row)],
        "probes": [row for row in filled if is_probe(row)],
    }
    result = {}
    for name, members in groups.items():
        settled = [value for row in members if (value := parse_decimal(row.get("realized_net_pnl"))) is not None]
        result[name] = {
            "filled": len(members),
            "settled": len(settled),
            "mean_realized_net_pnl": _rupees(settled),
            **{f"{kind}_ev": block for kind, block in _ev_block(members, measured).items()},
        }
    return result


def journal_high_water(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """How far the journal and its outcome resolver have got. Read from the rows already loaded.

    ``last_resolved_at`` is the loop's pulse: when it stops moving while decisions keep
    arriving, the forecasts are being written and never graded, and the sample the verdict
    waits on stops growing.
    """
    forecasts = [row for row in rows if row.get("forecast_probability_up")]
    decided = sorted(str(row["decided_at"]) for row in rows if row.get("decided_at"))
    resolved = sorted(str(row["resolved_at"]) for row in forecasts if row.get("resolved_at"))
    return {
        "rows": len(rows),
        "forecasts": len(forecasts),
        "resolved_forecasts": len(resolved),
        "last_decided_at": decided[-1] if decided else None,
        "last_resolved_at": resolved[-1] if resolved else None,
    }


def promotion_verdict(
    rows: Sequence[dict[str, Any]],
    *,
    realised_max_drawdown: Any = None,
    policy_max_drawdown: Any = None,
) -> dict[str, Any]:
    """The verdict this report reaches, without the rest of the report.

    One definition, so the dashboard's decision-quality report and this CLI cannot
    disagree about whether the same rows passed: the promotion report over the clean rows,
    overridden by ``basis_mixed`` whenever any row proves a second mapping.
    """
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
    return {"verdict": verdict, "promotion": promotion, "integrity": integrity, "clean": clean}


def calibrate(
    rows: Sequence[dict[str, Any]],
    *,
    realised_max_drawdown: Any = None,
    policy_max_drawdown: Any = None,
) -> dict[str, Any]:
    """The whole report: integrity first, then everything else over the clean rows only."""
    rows = list(rows)
    measured = empirical_payoffs.artifact(rows)
    reached = promotion_verdict(
        rows, realised_max_drawdown=realised_max_drawdown,
        policy_max_drawdown=policy_max_drawdown,
    )
    verdict, promotion = reached["verdict"], reached["promotion"]
    integrity, clean = reached["integrity"], reached["clean"]
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
        "payoffs": measured,
        # Declared and measured side by side, over the decisions that became positions and
        # over every forecast. The measured column never sizes, gates or replaces anything.
        "entered": entered(rows, measured),
        "expected_value": {
            **_ev_block([row for row in rows if row.get("forecast_probability_up")], measured),
            "declared_break_even_probability": declared_break_even(rows),
            "measured_break_even_probability": measured["overall"]["break_even_probability"],
        },
        "journal": journal_high_water(rows),
        "limitations": [
            "Scored only over rows whose probability is reproduced by one registered mapping.",
            "Unverifiable rows are excluded, not assumed clean; they predate the recorded inputs.",
            "The drawdown limit is rebuilt from the directives this process reads, as the warden does.",
            "This report arms nothing. The EV gate also needs measured, not declared, payoffs.",
            "Measured EV uses a symbol or playbook group only when it is not thin; it never sizes.",
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
    scored = promotion["resolved_forecast_count"]
    # Below forecast_scoring's own floor the base rate is set by whichever few rows
    # happened to resolve, and skill against it is a division by that accident: on the
    # first host run, two rows that moved the same way pinned the base rate at its 0.95
    # clamp and printed a skill of -114.9. The numbers stay in the JSON; the operator
    # text shows only what a sample this size can support.
    thin = scored < MINIMUM_SCORED
    lines += ["", "Scored sample" + (f"  ({scored} scored, below {MINIMUM_SCORED}: no claim)" if thin else "")]
    lines.append(_line("resolved forecasts (clean)", scored))
    lines.append(_line("minimum required", promotion["minimum_resolved"]))
    lines.append(_line("Brier", promotion["brier_score"]))
    lines.append(_line("Brier, coin (p=0.5)", promotion["brier_baseline_coin_flip"]))
    champion = next((item for item in report["scoring"]["by_basis"]
                     if item["basis"] == promotion["basis"]), None)
    base_rate = (champion or {}).get("baselines") or {}
    lines.append(_line("Brier, base rate",
                       None if thin else (base_rate.get("base_rate") or {}).get("brier_score")))
    lines.append(_line("skill vs base rate", None if thin else promotion["skill_vs_base_rate"]))
    bins = [item for item in promotion["reliability"] if item["forecasts"]]
    if bins:
        lines += ["", "Reliability (stated p against what happened)" + ("  (thin: no claim)" if thin else "")]
        lines += [f"  {item['lower']:.1f}-{item['upper']:.1f}  n={item['forecasts']:<5} "
                  f"mean p {_shown(item['mean_forecast'])}  observed {_shown(item['observed_frequency'])}"
                  for item in bins]
    lines += ["", "Book"]
    lines.append(_line("filled buys (probes count)", promotion["filled_buys"]))
    lines.append(_line("mean net P&L, filled buys", promotion["mean_net_ev_filled_buys"]))
    lines.append(_line("median |drift_bps|, fills", promotion["median_abs_entry_drift_bps"]))
    lines.append(_line("realised max drawdown", promotion["realised_max_drawdown"]))
    lines.append(_line("drawdown limit (live)", promotion["policy_max_drawdown"]))
    lines += ["", "Entries (filled buys; realised P&L in rupees, EV as a return on notional)"]
    for name, label in (("conviction", "conviction entries"), ("probes", "labelled probes")):
        group = report["entered"][name]
        lines.append(_line(label, (
            f"filled={group['filled']} settled={group['settled']} "
            f"mean net={_shown(group['mean_realized_net_pnl'])} "
            f"EV declared={_shown(group['declared_ev']['mean'])} "
            f"measured={_shown(group['empirical_ev']['mean'])}")))
    overall = report["payoffs"]["overall"]
    ev = report["expected_value"]
    lines += ["", "Payoffs, declared against measured (measured never sizes or gates)"]
    lines.append(_line("e_win_hat / e_loss_hat",
                       f"{_shown(overall['e_win_hat'])} / {_shown(overall['e_loss_hat'])}"))
    lines.append(_line("sample", f"{overall['n']}{' (thin)' if overall['thin'] else ''}"))
    lines.append(_line("break-even p (declared)", ev["declared_break_even_probability"]))
    lines.append(_line("break-even p (measured)", ev["measured_break_even_probability"]))
    for kind in ("declared", "empirical"):
        block = ev[kind]
        lines.append(_line(f"EV, every forecast ({'measured' if kind == 'empirical' else kind})",
                           f"n={block['n']} mean={_shown(block['mean'])} "
                           f"negative={_shown(block['negative_share'])}"))
    journal = report["journal"]
    lines += ["", "Journal"]
    lines.append(_line("rows / forecasts / resolver done",
                       f"{journal['rows']} / {journal['forecasts']} / {journal['resolved_forecasts']}"))
    lines.append(_line("last decided", journal["last_decided_at"]))
    lines.append(_line("last resolved", journal["last_resolved_at"]))
    if promotion["missing_inputs"]:
        lines += ["", "Missing inputs (the report refuses until these exist):"]
        lines += [f"  {code}: {MISSING_GLOSS.get(code, code)}" for code in promotion["missing_inputs"]]
    excluded = report["excluded"]
    if any(excluded.values()):
        lines += ["", "Excluded from scoring: " + ", ".join(
            f"{state}={count}" for state, count in excluded.items() if count)]
    return "\n".join(lines)
