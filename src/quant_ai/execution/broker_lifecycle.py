"""Read-only broker evidence reconciliation into durable OMS/account expectations.

Nothing here sends, modifies, cancels or repairs a broker order. Stable provider captures are
validated first. Known broker identities may advance the durable OMS from observation; an
uncertain submission without a broker order ID requires an explicit binding and is never
matched by symbol/side/quantity heuristics.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from quant_ai.execution.broker_journal import validate_capture
from quant_ai.execution.broker_observation import inspect_capture
from quant_ai.orders.oms import DurableOms, OmsOrder
from quant_ai.orders.state import OrderState


@dataclass(frozen=True)
class BrokerLifecycleIssue:
    code: str
    client_order_id: str | None = None
    broker_order_id: str | None = None


@dataclass(frozen=True)
class BrokerLifecycleReport:
    status: str
    capture_sha256: str
    resolved: tuple[str, ...]
    unresolved: tuple[BrokerLifecycleIssue, ...]
    issues: tuple[BrokerLifecycleIssue, ...]


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"invalid_{name}") from error
    if not result.is_finite():
        raise ValueError(f"invalid_{name}")
    return result


def _capture_instant(capture: Mapping[str, object]) -> datetime:
    return datetime.fromisoformat(str(capture["finishedAt"]))


def _trade_instant(trade: Mapping[str, object]) -> datetime:
    return datetime.fromisoformat(str(trade["at"]))


def _compatible(current: OmsOrder, broker_order: Mapping[str, object]) -> bool:
    return (
        current.symbol == broker_order.get("symbol")
        and current.side == broker_order.get("side")
        and current.requested_quantity == broker_order.get("quantity")
    )


def _fill_id(trade: Mapping[str, object]) -> str:
    exchange = str(trade["exchange"])
    trade_id = str(trade["tradeId"])
    return f"KITE:{exchange}:{trade_id}"


class OmsBrokerLifecycleReconciler:
    """Advance OMS state only from stable, account-bound broker evidence."""

    def reconcile(
        self,
        oms: DurableOms,
        capture: dict,
        *,
        tenant_id: str,
        account_ref: str,
        bindings: Mapping[str, str] | None = None,
    ) -> BrokerLifecycleReport:
        evidence = validate_capture(capture, tenant_id, account_ref)
        inspection = inspect_capture(evidence)
        if inspection["status"] not in {"consistent", "empty"}:
            return BrokerLifecycleReport(
                "unavailable", evidence["sha256"], (), (),
                (BrokerLifecycleIssue("broker_capture_not_consistent"),),
            )
        supplied = dict(bindings or {})
        candidates = {row.client_order_id: row for row in oms.open_orders(tenant_id)}
        for client_id in supplied:
            try:
                candidate = oms.get(client_id)
            except KeyError:
                continue
            if candidate.tenant_id == tenant_id:
                candidates[client_id] = candidate
        broker_orders = {str(row["orderId"]): row for row in evidence["orders"]}
        trades_by_order: dict[str, list[dict]] = {}
        for trade in evidence["trades"]:
            trades_by_order.setdefault(str(trade["orderId"]), []).append(trade)
        issues: list[BrokerLifecycleIssue] = []
        unresolved: list[BrokerLifecycleIssue] = []
        resolved: list[str] = []
        work: list[tuple[OmsOrder, str, dict, tuple[dict, ...]]] = []
        for client_id, current in sorted(candidates.items()):
            bound = current.broker_order_id or supplied.get(client_id)
            if current.broker_order_id and supplied.get(client_id) not in {None, current.broker_order_id}:
                issues.append(
                    BrokerLifecycleIssue(
                        "explicit_binding_conflicts_with_oms", client_id,
                        supplied.get(client_id),
                    )
                )
                continue
            if bound is None:
                unresolved.append(BrokerLifecycleIssue("broker_order_binding_required", client_id))
                continue
            broker_order = broker_orders.get(bound)
            if broker_order is None:
                unresolved.append(
                    BrokerLifecycleIssue("broker_order_not_observed", client_id, bound)
                )
                continue
            if not _compatible(current, broker_order):
                issues.append(
                    BrokerLifecycleIssue("broker_order_identity_mismatch", client_id, bound)
                )
                continue
            if current.state is OrderState.CREATED:
                issues.append(
                    BrokerLifecycleIssue("broker_order_seen_before_risk_approval", client_id, bound)
                )
                continue
            linked = tuple(
                sorted(
                    trades_by_order.get(bound, []),
                    key=lambda row: (str(row["at"]), str(row["tradeId"])),
                )
            )
            broker_filled = int(broker_order["filled"])
            if current.filled_quantity > broker_filled:
                issues.append(
                    BrokerLifecycleIssue(
                        "oms_filled_quantity_exceeds_broker", client_id, bound
                    )
                )
                continue
            known_fill_ids = set(oms.fill_ids(client_id))
            unseen = tuple(trade for trade in linked if _fill_id(trade) not in known_fill_ids)
            fill_delta = broker_filled - current.filled_quantity
            if fill_delta == 0:
                unseen = ()
            elif sum((int(trade["quantity"]) for trade in unseen), 0) != fill_delta:
                # Existing OMS fills may have come from an execution callback under provider-
                # independent IDs. Without exact trade lineage, replaying broker rows could
                # double count economic fills. Keep the order unresolved instead of guessing.
                issues.append(
                    BrokerLifecycleIssue("broker_fill_lineage_ambiguous", client_id, bound)
                )
                continue
            terminal_expected = {
                OrderState.FILLED: "COMPLETE",
                OrderState.CANCELLED: "CANCELLED",
                OrderState.REJECTED: "REJECTED",
            }.get(current.state)
            if terminal_expected is not None and broker_order["status"] != terminal_expected:
                issues.append(
                    BrokerLifecycleIssue(
                        "terminal_oms_broker_status_mismatch", client_id, bound
                    )
                )
                continue
            work.append((current, bound, broker_order, unseen))

        # Fail the whole mutation pass on identity/order-authority defects. Missing external
        # evidence remains unresolved and can be retried on a later capture.
        if issues:
            return BrokerLifecycleReport(
                "discrepancy", evidence["sha256"], (), tuple(unresolved), tuple(issues)
            )

        for current, broker_id, broker_order, linked in work:
            client_id = current.client_order_id
            state = current.state
            if state in {OrderState.RISK_APPROVED, OrderState.SUBMISSION_UNCERTAIN}:
                current = oms.submitted(
                    client_id, broker_order_id=broker_id,
                    now=_capture_instant(evidence),
                )
            elif (
                state in {OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED}
                and current.broker_order_id is None
            ):
                current = oms.bind_broker_identity(
                    client_id, broker_order_id=broker_id,
                    reason="stable_broker_observation",
                    now=_capture_instant(evidence),
                )

            for trade in linked:
                current = oms.fill(
                    client_id,
                    fill_id=_fill_id(trade),
                    quantity=int(trade["quantity"]),
                    price=_decimal(trade["price"], "broker_trade_price"),
                    broker_order_id=broker_id,
                    now=_trade_instant(trade),
                )

            status = str(broker_order["status"])
            current = oms.get(client_id)
            if status == "CANCELLED" and current.state not in {
                OrderState.CANCELLED, OrderState.FILLED, OrderState.REJECTED
            }:
                current = oms.cancel(
                    client_id, reason="broker_observed_cancelled",
                    now=_capture_instant(evidence),
                )
            elif status == "REJECTED" and current.state not in {
                OrderState.REJECTED, OrderState.FILLED, OrderState.CANCELLED
            }:
                current = oms.reject(
                    client_id, reason="broker_observed_rejected",
                    now=_capture_instant(evidence),
                )
            elif status == "COMPLETE" and current.state is not OrderState.FILLED:
                return BrokerLifecycleReport(
                    "discrepancy", evidence["sha256"], tuple(resolved), tuple(unresolved),
                    (BrokerLifecycleIssue("broker_complete_oms_not_filled", client_id, broker_id),),
                )
            resolved.append(client_id)

        return BrokerLifecycleReport(
            "matched" if not unresolved else "partial",
            evidence["sha256"], tuple(resolved), tuple(unresolved), (),
        )


@dataclass(frozen=True)
class ExpectedBrokerPosition:
    instrument_id: str
    symbol: str
    exchange: str
    product: str
    quantity: Decimal
    average_cost: Decimal

    def __post_init__(self) -> None:
        if not all((self.instrument_id, self.symbol, self.exchange, self.product)):
            raise ValueError("expected_broker_position_identity_required")
        if not self.quantity.is_finite() or not self.average_cost.is_finite():
            raise ValueError("expected_broker_position_amounts_must_be_finite")


@dataclass(frozen=True)
class ExpectedBrokerAccount:
    tenant_id: str
    account_ref: str
    cash_balance: Decimal
    available_balance: Decimal
    positions: tuple[ExpectedBrokerPosition, ...]

    def __post_init__(self) -> None:
        if not self.tenant_id or len(self.account_ref) != 64:
            raise ValueError("expected_broker_account_identity_invalid")
        if not self.cash_balance.is_finite() or not self.available_balance.is_finite():
            raise ValueError("expected_broker_funds_must_be_finite")
        identities = [
            (item.instrument_id, item.exchange, item.product) for item in self.positions
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate_expected_broker_position")


@dataclass(frozen=True)
class BrokerAccountReconciliation:
    status: str
    snapshot_sha256: str
    issues: tuple[str, ...]


def _external_snapshot_digest(snapshot: Mapping[str, object]) -> str:
    unsigned = {key: value for key, value in snapshot.items() if key != "sha256"}
    raw = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def reconcile_external_account(
    snapshot: dict,
    expected: ExpectedBrokerAccount,
) -> BrokerAccountReconciliation:
    sha = str(snapshot.get("sha256", ""))
    if sha != _external_snapshot_digest(snapshot):
        return BrokerAccountReconciliation("unavailable", sha, ("snapshot_hash_mismatch",))
    required = {
        "schema": "pramana.external_account_snapshot.v1",
        "broker": "zerodha-kite",
        "tenantId": expected.tenant_id,
        "accountRef": expected.account_ref,
    }
    for key, value in required.items():
        if snapshot.get(key) != value:
            return BrokerAccountReconciliation(
                "unavailable", sha, (f"snapshot_{key}_mismatch",)
            )
    if snapshot.get("status") != "consistent":
        return BrokerAccountReconciliation("unavailable", sha, ("snapshot_not_consistent",))
    if (
        snapshot.get("funds") != snapshot.get("fundsBefore")
        or snapshot.get("positions") != snapshot.get("positionsBefore")
    ):
        return BrokerAccountReconciliation(
            "unavailable", sha, ("snapshot_before_after_mismatch",)
        )

    funds = snapshot.get("funds")
    positions = snapshot.get("positions")
    if not isinstance(funds, dict) or not isinstance(positions, list):
        return BrokerAccountReconciliation("unavailable", sha, ("snapshot_shape_invalid",))
    issues: list[str] = []
    if _decimal(funds.get("cash_balance"), "external_cash_balance") != expected.cash_balance:
        issues.append("cash_balance_mismatch")
    if (
        _decimal(funds.get("available_balance"), "external_available_balance")
        != expected.available_balance
    ):
        issues.append("available_balance_mismatch")

    observed: dict[tuple[str, str, str], dict] = {}
    for row in positions:
        if not isinstance(row, dict):
            return BrokerAccountReconciliation(
                "unavailable", sha, ("snapshot_position_shape_invalid",)
            )
        key = (
            str(row.get("instrument_id", "")),
            str(row.get("exchange", "")),
            str(row.get("product", "")),
        )
        if not all(key) or key in observed:
            return BrokerAccountReconciliation(
                "unavailable", sha, ("snapshot_position_identity_invalid",)
            )
        observed[key] = row

    expected_map = {
        (item.instrument_id, item.exchange, item.product): item
        for item in expected.positions
    }
    for key in sorted(set(expected_map) | set(observed)):
        wanted = expected_map.get(key)
        actual = observed.get(key)
        identity = ":".join(key)
        if wanted is None:
            issues.append(f"unexpected_position:{identity}")
            continue
        if actual is None:
            issues.append(f"missing_position:{identity}")
            continue
        if actual.get("symbol") != wanted.symbol:
            issues.append(f"position_symbol_mismatch:{identity}")
        if _decimal(actual.get("quantity"), "external_position_quantity") != wanted.quantity:
            issues.append(f"position_quantity_mismatch:{identity}")
        if _decimal(actual.get("average_cost"), "external_position_average_cost") != wanted.average_cost:
            issues.append(f"position_average_cost_mismatch:{identity}")

    return BrokerAccountReconciliation(
        "matched" if not issues else "discrepancy", sha, tuple(issues)
    )
