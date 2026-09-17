"""Bounded data-only snapshots of supplied institutional requests and execution plans.

Only explicitly registered local data contracts may be reconstructed. No pickle,
model loader, payload-selected import, provider, broker or execution is invoked.
This preserves declared evidence, not authenticity of inputs or permission to trade.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from functools import cache, lru_cache
from types import UnionType
from typing import TYPE_CHECKING, Union, get_args, get_origin, get_type_hints

from quant_ai.agents.swarm import InstrumentBoundTradeProposal, TradeProposal
from quant_ai.decision.edge import EdgeEvidence
from quant_ai.domain.models import AssetClass, Instrument, Market, PortfolioSnapshot, RiskMode, Side
from quant_ai.execution.planner import (
    ExecutionAlgorithm,
    ExecutionConstraints,
    ExecutionPlan,
    ExecutionSlice,
    VolumeBucket,
)
from quant_ai.execution.risk_authority import parse_authority
from quant_ai.orders.intent import order_from_snapshot
from quant_ai.planning.capital import CapitalPlan, GoalBand
from quant_ai.portfolio.optimizer import PortfolioOptimizationPolicy, StrategyOpportunity
from quant_ai.risk.factor_liquidity import FactorLiquidityPolicy, FactorLiquidityPosition

if TYPE_CHECKING:
    from quant_ai.execution.institutional import InstitutionalTradeRequest

SCHEMA = "pramana.institutional_request_context.v1"
MAX_BYTES = 1_000_000
MAX_NODES = 50_000
MAX_DEPTH = 32


class ExecutionContextError(ValueError):
    pass


def _check(ok, reason):
    if not ok:
        raise ExecutionContextError("execution_context_" + reason)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


@lru_cache(maxsize=1)
def _registry():
    # Lazy fixed import avoids a coordinator/journal module cycle. Payloads cannot
    # choose a module, constructor or arbitrary class outside this closed registry.
    from quant_ai.execution.institutional import InstitutionalTradeRequest
    classes = (InstitutionalTradeRequest, TradeProposal, InstrumentBoundTradeProposal,
        EdgeEvidence, Instrument, PortfolioSnapshot, CapitalPlan, GoalBand,
        PortfolioOptimizationPolicy, StrategyOpportunity, FactorLiquidityPolicy,
        FactorLiquidityPosition, ExecutionConstraints, ExecutionPlan, ExecutionSlice,
        VolumeBucket, AssetClass, Market, RiskMode, Side, ExecutionAlgorithm)
    return {c.__name__: c for c in classes}


@cache
def _hints(cls):
    return get_type_hints(cls)


def _matches(value, annotation):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        return any(_matches(value, a) for a in args)
    if annotation is type(None):
        return value is None
    if annotation in (dict, Mapping) or origin in (dict, Mapping):
        return isinstance(value, Mapping) and (not args or all(
            _matches(k, args[0]) and _matches(v, args[1]) for k, v in value.items()))
    if annotation in (tuple, list) or origin in (tuple, list):
        cls = origin or annotation
        if type(value) is not cls:
            return False
        if not args:
            return True
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches(v, args[0]) for v in value)
        return len(value) == len(args) and all(_matches(v, a) for v, a in zip(value, args))
    if annotation is TradeProposal:
        return type(value) in (TradeProposal, InstrumentBoundTradeProposal)
    return type(value) is annotation


def _record(cls, values):
    _check(set(values) == {f.name for f in fields(cls)}, "record_fields_invalid")
    hints = _hints(cls)
    _check(all(_matches(v, hints[k]) for k, v in values.items()), "record_types_invalid")
    try:
        return cls(**values)
    except (TypeError, ValueError, ArithmeticError, AttributeError) as error:
        raise ExecutionContextError("execution_context_record_contract_invalid") from error


class _Codec:
    def __init__(self):
        self.nodes = 0

    def tick(self, depth):
        self.nodes += 1
        _check(depth <= MAX_DEPTH and self.nodes <= MAX_NODES, "complexity_limit")

    def encode(self, value, depth=0):
        self.tick(depth)
        child = lambda v: self.encode(v, depth + 1)
        cls = type(value)
        if value is None:
            return ["none"]
        if isinstance(value, Enum):
            _check(_registry().get(cls.__name__) is cls, "unregistered_enum")
            return ["enum", cls.__name__, value.value]
        if cls is bool:
            return ["bool", value]
        if cls is int:
            _check(abs(value) <= 2**53 - 1, "integer_bounds")
            return ["int", str(value)]
        if cls is str:
            _check(len(value) <= MAX_BYTES, "string_bounds")
            return ["str", value]
        if cls is float:
            _check(math.isfinite(value), "nonfinite_number")
            return ["float", repr(value)]
        if cls is Decimal:
            _check(value.is_finite() and len(str(value)) <= 128
                   and abs(value.as_tuple().exponent) <= 1000, "decimal_bounds")
            return ["decimal", str(value)]
        if cls is datetime:
            _check(value.utcoffset() is not None, "aware_timestamp_required")
            return ["datetime", value.isoformat()]
        if cls is date:
            return ["date", value.isoformat()]
        if is_dataclass(value) and not isinstance(value, type):
            _check(_registry().get(cls.__name__) is cls, "unregistered_record")
            values = {f.name: getattr(value, f.name) for f in fields(value)}
            _record(cls, values)
            return ["record", cls.__name__, {k: child(v) for k, v in values.items()}]
        if isinstance(value, Mapping):
            pairs = [[child(k), child(v)] for k, v in value.items()]
            return ["map", sorted(pairs, key=lambda pair: _json(pair[0]))]
        if cls in (tuple, list):
            return [cls.__name__, [child(v) for v in value]]
        raise ExecutionContextError("execution_context_unsupported_value")

    def decode(self, value, depth=0):
        self.tick(depth)
        _check(type(value) is list and value and type(value[0]) is str, "node_invalid")
        tag = value[0]
        arity = 1 if tag == "none" else 3 if tag in {"record", "enum"} else 2
        _check(len(value) == arity, "node_shape_invalid")
        child = lambda v: self.decode(v, depth + 1)
        if tag == "none":
            return None
        item = value[1]
        if tag == "bool":
            _check(type(item) is bool, "boolean_invalid")
            return item
        if tag == "str":
            _check(type(item) is str, "string_invalid")
            return item
        if tag in {"int", "decimal", "float", "datetime", "date"}:
            _check(type(item) is str and len(item) <= 128, "scalar_invalid")
            parsers = {"int": int, "decimal": Decimal, "float": float,
                       "datetime": datetime.fromisoformat, "date": date.fromisoformat}
            return parsers[tag](item)
        if tag in {"tuple", "list"}:
            _check(type(item) is list, "sequence_invalid")
            decoded = [child(v) for v in item]
            return tuple(decoded) if tag == "tuple" else decoded
        if tag == "map":
            _check(type(item) is list, "mapping_invalid")
            result = {}
            for pair in item:
                _check(type(pair) is list and len(pair) == 2, "mapping_pair_invalid")
                k, v = child(pair[0]), child(pair[1])
                _check(k not in result, "duplicate_mapping_key")
                result[k] = v
            return result
        _check(tag in {"record", "enum"} and type(item) is str and item in _registry(),
               "unknown_type")
        cls, raw = _registry()[item], value[2]
        if tag == "enum":
            _check(issubclass(cls, Enum) and type(raw) is str, "enum_invalid")
            return cls(raw)
        _check(is_dataclass(cls) and type(raw) is dict, "record_invalid")
        return _record(cls, {k: child(v) for k, v in raw.items()})


def _unique(pairs):
    result = {}
    for k, v in pairs:
        _check(k not in result, "duplicate_json_key")
        result[k] = v
    return result


@dataclass(frozen=True)
class StoredExecutionContext:
    request: InstitutionalTradeRequest
    plan: ExecutionPlan


def encode_context(request, plan):
    _check(type(request) is _registry()["InstitutionalTradeRequest"]
           and type(plan) is ExecutionPlan, "root_types_invalid")
    codec = _Codec()
    payload = {"schema": SCHEMA, "request": codec.encode(request), "plan": codec.encode(plan)}
    raw = _json(payload)
    _check(len(raw.encode()) <= MAX_BYTES, "size_limit")
    return raw


def decode_context(raw):
    try:
        _check(type(raw) is str and len(raw.encode()) <= MAX_BYTES, "size_limit")
        payload = json.loads(raw, object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ExecutionContextError("execution_context_nonfinite_json")))
        _check(type(payload) is dict and set(payload) == {"schema", "request", "plan"}
               and payload["schema"] == SCHEMA, "schema_invalid")
        codec = _Codec()
        request, plan = codec.decode(payload["request"]), codec.decode(payload["plan"])
        _check(encode_context(request, plan) == raw, "noncanonical_payload")
        plan.assert_conservative()
        return StoredExecutionContext(request, plan)
    except ExecutionContextError:
        raise
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError, RecursionError) as error:
        raise ExecutionContextError("execution_context_invalid_payload") from error


def validate_context(version, raw, *, program_id, tenant_id, parent_payload,
                     authority_payload, runtime_digest, plan_digest, slices, decision_id, created_at,
                     payload_sha256=None):
    if type(version) is int and version == 0:
        _check(raw is None and payload_sha256 is None, "legacy_payload_contradiction")
        return None
    _check(type(version) is int and version == 1 and raw is not None, "version_or_payload_invalid")
    _check(type(raw) is str and len(raw.encode()) <= MAX_BYTES
           and type(payload_sha256) is str
           and payload_sha256 == hashlib.sha256(raw.encode()).hexdigest(),
           "payload_digest_mismatch")
    stored = decode_context(raw)
    from quant_ai.execution.institutional import request_fingerprint
    from quant_ai.execution.program import ExecutionProgramJournal
    from quant_ai.execution.risk_authority import validate_authority
    validate_authority(1, authority_payload, program_id=program_id, tenant_id=tenant_id,
        parent_payload=parent_payload, runtime_digest=runtime_digest)
    authority = parse_authority(authority_payload)
    request, plan = stored.request, stored.plan
    parent = order_from_snapshot(parent_payload)
    _check(request_fingerprint(request) == authority["requestSha256"], "request_digest_mismatch")
    _check(hashlib.sha256(_json(ExecutionProgramJournal._plan_payload(plan)).encode()).hexdigest()
           == plan_digest, "plan_digest_mismatch")
    _check(request.tenant_id == tenant_id == parent.tenant_id
           and request.proposal.decision_id == decision_id
           and request.proposal.symbol == parent.symbol
           and request.proposal.market is parent.market
           and request.proposal.asset_class is parent.asset_class
           and request.proposal.side is parent.side
           and request.strategy_id == parent.strategy_id
           and request.observed_at == created_at
           and plan.algorithm is request.execution_algorithm
           and plan.parent_quantity == parent.quantity == request.proposal.quantity
           and request.proposal.reference_price == parent.reference_price
           and request.proposal.stop_price == parent.stop_price
           and request.proposal.take_profit_price == parent.take_profit_price
           and getattr(request.proposal, "instrument", None) == getattr(parent, "instrument", None),
           "identity_mismatch")
    constraints = request.execution_constraints
    _check(plan.lot_size == constraints.lot_size and plan.lot_size > 0 and plan.source.strip(),
           "plan_constraints_mismatch")
    buckets = request.volume_buckets
    _check(buckets and all(a.at < b.at for a, b in zip(buckets, buckets[1:])), "liquidity_schedule_invalid")
    volumes = {b.at: b.available_quantity for b in buckets}
    used = set()
    for item in plan.slices:
        volume = volumes.get(item.at)
        _check(item.at not in used and volume is not None and volume > 0
               and item.observed_available_quantity == volume
               and item.participation == Decimal(item.quantity) / Decimal(volume)
               and item.participation <= constraints.max_participation
               and item.quantity >= (constraints.min_child_quantity or constraints.lot_size)
               and (constraints.max_child_quantity is None or item.quantity <= constraints.max_child_quantity),
               "plan_liquidity_mismatch")
        used.add(item.at)
    expected = [(s.sequence, s.at.astimezone(timezone.utc), s.quantity) for s in plan.slices]
    _check(slices == expected, "schedule_mismatch")
    return stored
