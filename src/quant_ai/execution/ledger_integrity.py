"""Shared validation for stored paper quantities and monetary values."""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

from quant_ai.domain.models import AssetClass, Market


class PaperLedgerDataError(ValueError):
    """Stored accounting data cannot safely be used; the source is never repaired."""


def finite_amount(value, code: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
        valid = (not isinstance(value, bool) and result.is_finite()
                 and math.isfinite(float(result)))
        if valid and positive:
            valid = result > 0 and float(result) > 0
        if valid and nonnegative:
            valid = result >= 0
        if valid:
            return result
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        pass
    raise PaperLedgerDataError(code)


def whole_quantity(value, code: str = "invalid_position_quantity") -> int:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise PaperLedgerDataError(code)
    return value


def position_geometry_issues(row) -> list[str]:
    issues = []
    if not isinstance(row["symbol"], str) or not row["symbol"].strip():
        issues.append("invalid_position_symbol")
    try:
        Market(row["market"])
        AssetClass(row["asset_class"])
    except (ValueError, TypeError):
        issues.append("invalid_position_identity")
    try:
        whole_quantity(row["quantity"])
    except PaperLedgerDataError as error:
        issues.append(str(error))
    try:
        finite_amount(row["average_price"], "invalid_position_average", positive=True)
    except PaperLedgerDataError as error:
        issues.append(str(error))
    return issues
