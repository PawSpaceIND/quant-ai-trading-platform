"""Single-period Brinson-Fachler arithmetic attribution from reviewed inputs.

Beginning weights and same-period total returns, one currency, long-only and
fully invested including cash. No inferred sector returns or multi-period linking.
"""
from decimal import Decimal, InvalidOperation, localcontext


def benchmark_attribution(document: dict) -> dict:
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def number(value):
        require(isinstance(value, str) and len(value) <= 80, "decimal_strings_required")
        try:
            result = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("invalid_decimal") from error
        require(result.is_finite() and abs(result) <= 1000, "invalid_decimal")
        require(result.as_tuple().exponent >= -18, "excess_decimal_precision")
        return result

    require(isinstance(document, dict), "invalid_document")
    require(document.get("schema") == "pramana.benchmark_attribution_input.v1", "invalid_schema")
    require(document.get("basis") == "beginning_weights_same_period_total_returns", "unsupported_basis")
    require(document.get("currency") == "INR", "unsupported_currency")
    rows = document.get("sectors")
    require(isinstance(rows, list) and 0 < len(rows) <= 500, "invalid_sectors")
    with localcontext() as context:
        context.prec = 80
        parsed, names = [], set()
        for row in rows:
            require(isinstance(row, dict), "invalid_sector")
            name = row.get("name")
            require(isinstance(name, str) and name == name.strip() and 0 < len(name) <= 100 and name not in names, "invalid_or_duplicate_sector")
            names.add(name)
            wp, wb, rp, rb = [number(row.get(key)) for key in
                              ("portfolioWeight", "benchmarkWeight", "portfolioReturn", "benchmarkReturn")]
            require(0 <= wp <= 1 and 0 <= wb <= 1, "invalid_weight")
            require(rp >= -1 and rb >= -1, "invalid_long_only_return")
            parsed.append((name, wp, wb, rp, rb))
        require(sum(r[1] for r in parsed) == 1 and sum(r[2] for r in parsed) == 1, "weights_must_sum_to_one_include_cash")
        portfolio_return = sum(wp * rp for _, wp, _, rp, _ in parsed)
        benchmark_return = sum(wb * rb for _, _, wb, _, rb in parsed)
        require(portfolio_return == number(document.get("portfolioReturn")), "portfolio_return_not_reconciled")
        require(benchmark_return == number(document.get("benchmarkReturn")), "benchmark_return_not_reconciled")
        effects = []
        for name, wp, wb, rp, rb in parsed:
            allocation = (wp - wb) * (rb - benchmark_return)
            selection = wb * (rp - rb)
            interaction = (wp - wb) * (rp - rb)
            effects.append({"name": name, "allocation": allocation, "selection": selection,
                            "interaction": interaction, "total": allocation + selection + interaction})
        totals = {key: sum(row[key] for row in effects) for key in ("allocation", "selection", "interaction", "total")}
        active = portfolio_return - benchmark_return
        require(totals["total"] == active, "attribution_not_reconciled")
        return {"schema": "pramana.benchmark_attribution.v1", "method": "single_period_brinson_fachler",
                "currency": "INR", "portfolioReturn": str(portfolio_return), "benchmarkReturn": str(benchmark_return),
                "activeReturn": str(active), "reconciliationDifference": str(totals["total"] - active),
                "sectors": [{key: str(value) if isinstance(value, Decimal) else value for key, value in row.items()} for row in effects],
                "totals": {key: str(value) for key, value in totals.items()},
                "scope": "Arithmetic verification of supplied inputs; source qualification and dashboard publication remain separate."}
