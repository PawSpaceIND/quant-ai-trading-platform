"""Review-only reports: no ranking, promotion, network access or trade execution."""

from __future__ import annotations

from decimal import Decimal
from html import escape


def diagnostics(report):
    """Expose comparability blockers; an empty list does not certify performance."""
    issues = []
    if report["registered_cases"] < report["config"]["expected_cases"]:
        issues.append("case_collection_incomplete")
    if report["resolved_cases"] < report["registered_cases"]:
        issues.append("outcomes_incomplete")
    if report["unverified_adjustments"]:
        issues.append("corporate_action_adjustments_unverified")
    for name, candidate in report["candidates"].items():
        for key in ("errors", "missing_decisions", "pending_buy_outcomes", "unresolved_exits"):
            if candidate[key]:
                issues.append(f"{name}:{key}")
        if candidate.get("unknown_cost_decisions", 0):
            issues.append(f"{name}:unknown_cost_decisions")
    return {
        "comparison_blockers": issues,
        "winner": None,
        "promotion_allowed": False,
        "requires_independent_review": True,
    }


def cost_stress(config, decision, outcome, multipliers=(1, 2, 3)):
    """Retrospective cost sensitivity only, not another candidate selection trial.

    Liquidity/cash caps are recalculated under each declared scenario. Consequently
    quantity may change. Missing exits remain unresolved; no return is invented.
    """
    from quant_ai.research.lab import number, simulate

    results = []
    for multiplier in multipliers:
        factor = number(multiplier, positive=True)
        if factor < 1:
            raise ValueError("stress_must_not_reduce_costs")
        stress = dict(config)
        for key in ("fee_bps", "slippage_bps"):
            value = number(config[key]) * factor
            if value >= Decimal(10000):
                raise ValueError("invalid_cost_assumption")
            stress[key] = str(value)
        results.append({"multiplier": str(factor), **simulate(stress, decision, outcome)})
    return results


def render_html(report):
    """Standalone escaped, script-free review view; never embeds raw input packets."""

    def e(value):
        return escape(str(value), quote=True)

    columns = [
        ("decisions", "Decisions"),
        ("missing_decisions", "Missing decisions"),
        ("errors", "Errors"),
        ("holds", "No-trade decisions"),
        ("completed_episodes", "Completed cases"),
        ("unfilled", "Unfilled"),
        ("partial_entries", "Partial entries"),
        ("unresolved_exits", "Unresolved exits"),
        ("pending_buy_outcomes", "Pending buys"),
        ("completed_case_pnl_inr", "Completed-case P&L (INR)"),
        ("api_cost_usd", "Known API cost subtotal (USD)"),
        ("unknown_cost_decisions", "Decisions with unknown cost"),
    ]
    rows = "".join(
        "<tr><th scope='row'>"
        + e(name)
        + "</th>"
        + "".join("<td>" + e(candidate.get(key, 0)) + "</td>" for key, _ in columns)
        + "</tr>"
        for name, candidate in report["candidates"].items()
    )
    headers = "<th scope='col'>Candidate</th>" + "".join(
        "<th scope='col'>" + e(label) + "</th>" for _, label in columns
    )
    checks = diagnostics(report)
    blockers = checks["comparison_blockers"] or [
        "No completeness blockers; independent review required"
    ]
    limitations = "".join("<li>" + e(item) + "</li>" for item in report["limitations"])
    details = []
    for name, candidate in report["candidates"].items():
        records = "".join(
            "<tr><td>"
            + e(item["case_id"])
            + "</td><td>"
            + e(item["status"])
            + "</td><td>"
            + e(item["filled_quantity"])
            + "</td><td>"
            + e(item["net_pnl_inr"] if item["net_pnl_inr"] is not None else "Unavailable")
            + "</td></tr>"
            for item in candidate["outcomes"]
        )
        details.append(
            "<details><summary>" + e(name) + " — execution cases</summary>"
            "<table><thead><tr><th>Case</th><th>Status</th><th>Quantity</th>"
            "<th>P&amp;L (INR)</th></tr></thead><tbody>" + records + "</tbody></table></details>"
        )
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Pramana research review</title>
<style>body{font:16px system-ui;margin:2rem auto;padding:0 1rem;max-width:1200px;
background:#101820;color:#eef4f7}h1{font-size:1.8rem}p,li{line-height:1.6}
.notice{padding:1rem;border:1px solid #d4af64;border-radius:8px;background:#29261f}
.scroll{overflow:auto}table{border-collapse:collapse;width:100%;margin:1rem 0}
th,td{padding:.7rem;text-align:left;border-bottom:1px solid #40505e;white-space:nowrap}
summary{padding:1rem;cursor:pointer}details{border:1px solid #40505e;margin:1rem 0}
small{color:#bdcbd6}</style></head><body><main><h1>Pramana research review</h1>""" + (
        "<p>Experiment: <strong>"
        + e(report["experiment"])
        + "</strong> · "
        + e(report["config"]["mode"])
        + "</p><p class='notice'><strong>Insufficient evidence. "
        "No winner selected; no trading permission.</strong><br>Independent case simulations, "
        "not portfolio returns. Unresolved cases can hide losses. API costs are separate.</p>"
        "<p>Registered cases: "
        + e(report["registered_cases"])
        + " / "
        + e(report["config"]["expected_cases"])
        + "; recorded outcomes: "
        + e(report["resolved_cases"])
        + "</p><h2>Comparison completeness</h2><ul>"
        + "".join("<li>" + e(item) + "</li>" for item in blockers)
        + "</ul>"
        "<div class='scroll'><table><caption>Candidate evidence summary</caption><thead><tr>"
        + headers
        + "</tr></thead><tbody>"
        + rows
        + "</tbody></table></div>"
        + "".join(details)
        + "<h2>Assumptions and limits</h2><ul>"
        + limitations
        + "</ul><small>Offline review view. No network calls, credentials or order controls.</small>"
        "</main></body></html>"
    )
