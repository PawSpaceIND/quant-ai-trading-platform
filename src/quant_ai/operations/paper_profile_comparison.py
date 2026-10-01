"""Offline fixed-versus-dynamic research cohorts; no runtime or portfolio simulation."""
from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from zoneinfo import ZoneInfo

from quant_ai.learning.feature_dataset import canonical
from quant_ai.learning.forecast_evaluation import _digest, _metrics
from quant_ai.learning.shadow import _instant

SHA = re.compile(r'[0-9a-f]{64}')
ID = re.compile(r'[A-Za-z0-9._:/-]{1,100}')
PLAN_FIELDS = {'schema', 'mode', 'venue', 'event', 'frozen_at', 'start', 'end', 'catalog', 'fixed_symbols',
               'dynamic_limit', 'horizon_seconds', 'strategy_sha256', 'risk_policy_sha256', 'cost_policy_sha256',
               'dynamic_rule_sha256'}
CYCLE_FIELDS = {'cycle_id', 'plan_sha256', 'decision_at', 'selection_cutoff', 'selection_available_at',
                'selection_sha256', 'dynamic_selected', 'opportunities'}
ROW_FIELDS = {'symbol', 'input_available_at', 'proof_sha256', 'resolve_after', 'outcome', 'profiles'}
PROFILE_FIELDS = {'status', 'probability', 'forecast_available_at', 'reasons'}
OUTCOME_FIELDS = {'resolved_at', 'gross_return', 'cost_return', 'evidence_sha256'}
STATUSES = {'UNSELECTED', 'HOLD', 'REJECTION', 'PROPOSAL'}


def _require(condition, reason):
    if not condition:
        raise ValueError('paper_comparison_' + reason)


def _fields(value, expected):
    _require(type(value) is dict and set(value) == expected, 'fields')


def _sha(value):
    _require(type(value) is str and SHA.fullmatch(value), 'digest')


def _names(value, *, limit):
    _require(type(value) is list and len(value) <= limit
             and all(type(s) is str and ID.fullmatch(s) for s in value)
             and len(set(value)) == len(value), 'symbols')
    return set(value)


def _number(value, low, high):
    _require(type(value) is str and len(value) <= 80 and value == value.strip(), 'number')
    try:
        result = Decimal(value)
    except ArithmeticError as error:
        raise ValueError('paper_comparison_number') from error
    _require(result.is_finite() and low <= result <= high, 'number_bound')
    return result


def _summary(rows):
    # Missing forecasts/outcomes stay in denominators, never become a zero label.
    resolved = [r for r in rows if r['net'] is not None]
    scored = [r for r in resolved if r['probability'] is not None]
    probabilities = [r['probability'] for r in scored]
    targets = [Decimal(int(r['net'] > 0)) for r in scored]
    metrics = _metrics(probabilities, targets) if scored else None
    baseline = _metrics([Decimal('0.5')] * len(scored), targets) if scored else None
    return {'selected_observations': len(rows), 'resolved_opportunities': len(resolved),
            'unresolved_opportunities': len(rows) - len(resolved),
            'scored_forecasts': len(scored), 'resolved_without_forecast': len(resolved) - len(scored),
            'statuses': dict(sorted(Counter(r['status'] for r in rows).items())),
            'reasons': dict(sorted(Counter(x for r in rows for x in r['reasons']).items())),
            'mean_after_cost_opportunity_return':
                str(sum((r['net'] for r in resolved), Decimal(0)) / len(resolved)) if resolved else None,
            'mean_cost_return':
                str(sum((r['cost'] for r in resolved), Decimal(0)) / len(resolved)) if resolved else None,
            'forecast_metrics': metrics, 'constant_half_baseline': baseline}


def compare_paper_profiles(plan, cycles, *, now):
    """Validate complete recorded catalog cycles and score declared research cohorts.

    The profile declaration freezes identical strategy/risk/cost fingerprints; only
    universe membership differs. These are caller claims, not authenticated logs.
    Dynamic selections must already have been available at the decision boundary.
    No model is fit, decision generated, provider called, database opened or order
    inferred from a recorded proposal. Opportunity returns are never portfolio P&L.
    """
    _fields(plan, PLAN_FIELDS)
    _require(plan['schema'] == 'pramana.paper_profile_plan.v1' and plan['mode'] == 'PAPER'
             and plan['venue'] == 'NSE_CASH' and plan['event'] == 'positive_long_return_after_cost', 'paper_plan')
    frozen, start, end, now = (_instant(x) for x in (plan['frozen_at'], plan['start'], plan['end'], now))
    _require(frozen < start < end and start <= now, 'window')
    _require(end - start <= timedelta(days=90), 'window_bound')
    catalog = _names(plan['catalog'], limit=50)
    fixed = _names(plan['fixed_symbols'], limit=50)
    _require(bool(fixed) and fixed <= catalog, 'fixed_catalog')
    _require(type(plan['dynamic_limit']) is int and 1 <= plan['dynamic_limit'] <= 15, 'dynamic_limit')
    for name in ('strategy_sha256', 'risk_policy_sha256', 'cost_policy_sha256', 'dynamic_rule_sha256'):
        _sha(plan[name])
    _require(type(cycles) is list and 1 <= len(cycles) <= 2000
             and len(cycles) * len(catalog) <= 10000, 'cycle_bound')
    _require(type(plan['horizon_seconds']) is int and 1 <= plan['horizon_seconds'] <= 2592000, 'horizon')
    plan_digest = _digest(canonical(plan))
    cohorts = {'fixed': [], 'dynamic': []}
    prior_decision = None
    paired, seen, selection_counts = [], set(), []
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        for cycle in cycles:
            _fields(cycle, CYCLE_FIELDS)
            _require(cycle['plan_sha256'] == plan_digest, 'plan_binding')
            identity = cycle['cycle_id']
            _require(type(identity) is str and ID.fullmatch(identity) and identity not in seen, 'cycle_identity')
            seen.add(identity)
            decision = _instant(cycle['decision_at'])
            _require(prior_decision is None or prior_decision < decision, 'cycle_order')
            prior_decision = decision
            cutoff, available = (_instant(cycle[k]) for k in ('selection_cutoff', 'selection_available_at'))
            _require(start <= decision < end and decision <= now and cutoff <= available <= decision,
                     'selection_time')
            # This contract compares prior-available research, never current-session winner picking.
            _require(cutoff.astimezone(ZoneInfo('Asia/Kolkata')).date()
                     < decision.astimezone(ZoneInfo('Asia/Kolkata')).date(), 'prior_session_cutoff')
            _sha(cycle['selection_sha256'])
            dynamic = _names(cycle['dynamic_selected'], limit=plan['dynamic_limit'])
            _require(dynamic <= catalog, 'dynamic_catalog')
            selection_counts.append({'cycle_id': identity, 'fixed': len(fixed), 'dynamic': len(dynamic),
                                     'selection_age_seconds': str((decision - available).total_seconds())})
            opportunities = cycle['opportunities']
            _require(type(opportunities) is list and len(opportunities) == len(catalog), 'complete_catalog_cycle')
            subjects = set()
            for row in opportunities:
                _fields(row, ROW_FIELDS)
                symbol = row['symbol']
                _require(type(symbol) is str and symbol in catalog and symbol not in subjects, 'opportunity_identity')
                subjects.add(symbol)
                _sha(row['proof_sha256'])
                resolve = _instant(row['resolve_after'])
                _require(resolve == decision + timedelta(seconds=plan['horizon_seconds']), 'resolution_horizon')
                input_known = row['input_available_at'] is not None
                if input_known:
                    _require(_instant(row['input_available_at']) <= decision, 'future_input')
                net = cost = None
                if row['outcome'] is not None:
                    outcome = row['outcome']
                    _fields(outcome, OUTCOME_FIELDS)
                    _sha(outcome['evidence_sha256'])
                    _require(resolve <= _instant(outcome['resolved_at']) <= now, 'outcome_time')
                    cost = _number(outcome['cost_return'], Decimal(0), Decimal(1))
                    net = _number(outcome['gross_return'], Decimal(-1), Decimal(10)) - cost
                _fields(row['profiles'], {'fixed', 'dynamic'})
                observed = {}
                for profile, members in (('fixed', fixed), ('dynamic', dynamic)):
                    item = row['profiles'][profile]
                    _fields(item, PROFILE_FIELDS)
                    _require(type(item['status']) is str and item['status'] in STATUSES, 'status')
                    selected = symbol in members
                    _require((item['status'] != 'UNSELECTED') == selected, 'membership_status')
                    reasons = item['reasons']
                    _require(type(reasons) is list and len(reasons) <= 20
                             and all(type(r) is str and ID.fullmatch(r) for r in reasons)
                             and len(set(reasons)) == len(reasons), 'reasons')
                    _require(item['status'] not in {'HOLD', 'REJECTION'} or bool(reasons), 'refusal_reason')
                    probability = None if item['probability'] is None else _number(item['probability'], Decimal(0), Decimal(1))
                    if probability is None:
                        _require(item['forecast_available_at'] is None, 'forecast_time_without_probability')
                    else:
                        _require(item['forecast_available_at'] is not None
                                 and _instant(item['forecast_available_at']) <= decision, 'future_forecast')
                    _require(selected or probability is None, 'unselected_forecast')
                    _require(input_known or (probability is None and item['status'] != 'PROPOSAL'), 'unknown_input')
                    if selected:
                        value = {'status': item['status'], 'reasons': reasons, 'probability': probability,
                                 'net': net, 'cost': cost}
                        cohorts[profile].append(value)
                        observed[profile] = value
                if (set(observed) == {'fixed', 'dynamic'} and net is not None
                        and all(observed[p]['probability'] is not None for p in observed)):
                    paired.append((observed['fixed']['probability'], observed['dynamic']['probability'], net))
        summaries = {name: _summary(rows) for name, rows in cohorts.items()}
        matched = None
        if paired:
            targets = [Decimal(int(net > 0)) for _, _, net in paired]
            left = _metrics([p for p, _, _ in paired], targets)
            right = _metrics([p for _, p, _ in paired], targets)
            matched = {'pairs': len(paired), 'fixed': left, 'dynamic': right,
                       'dynamic_brier_improvement': str(Decimal(left['brier_score']) - Decimal(right['brier_score'])),
                       'dynamic_log_loss_improvement': str(Decimal(left['log_loss']) - Decimal(right['log_loss']))}
    result = {'schema': 'pramana.paper_profile_comparison.v1', 'plan_sha256': plan_digest,
              'observations_sha256': _digest(canonical(cycles)), 'as_of': now.isoformat(),
              'window_complete': now >= end, 'recorded_cycles': len(cycles),
              'catalog_observations': len(cycles) * len(catalog), 'selection_counts': selection_counts,
              'cohorts': summaries, 'matched_forecasts': matched,
              'trading_authorized': False, 'activation_authorized': False, 'model_updated': False,
              'portfolio_returns_evaluated': False, 'source_authenticity_verified': False,
              'prospective_registration_verified': False, 'scheduler_completeness_verified': False,
              'matched_sample_at_least_30': len(paired) >= 30,
              'limitations': ['Caller-declared frozen profiles/evidence are not authenticated prospective registration.',
                              'Different selected cohorts have different outcomes; their means are not paired skill or portfolio returns.',
                              'Only common resolved opportunities with both forecasts enter matched calibration.',
                              'Pending/missing forecasts remain counted; no forecast is reconstructed or model trained.',
                              'Recorded proposals are not orders/fills; execution conservation needs separate ledger reconciliation.',
                              'No winner, statistical significance, profitability or live-readiness verdict is produced.']}
    result['sha256'] = _digest(canonical(result))
    return result


def _mean(values):
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        return str(sum(values, Decimal(0)) / len(values)) if values else None


def summarize_recorded_model_pairs(pairs, *, start, end, now):
    """Describe existing Claude/OpenAI observations; never generate a paid comparison.

    Confidence is not a calibrated probability. This summary deliberately reports no
    forecast skill, investment winner, billed cost or execution authority change.
    """
    import math

    start, end, now = (_instant(v) for v in (start, end, now))
    _require(start < end and start <= now and end - start <= timedelta(days=90), 'model_window')
    _require(type(pairs) is list and len(pairs) <= 10000, 'model_pair_bound')
    seen, refused, statuses = set(), Counter(), {'primary': Counter(), 'challenger': Counter()}
    latencies, usage = {'primary': [], 'challenger': []}, {'primary': [], 'challenger': []}
    matched = agree = 0
    for pair in pairs:
        _require(type(pair) is dict and pair.get('schema') == 'pramana.model_comparison.v1', 'model_pair_schema')
        identity = pair.get('pair_id')
        _require(type(identity) is str and ID.fullmatch(identity) and identity not in seen, 'model_pair_identity')
        seen.add(identity)
        observed = _instant(pair.get('observed_at'))
        _require(start <= observed < end and observed <= now, 'model_pair_time')
        _require(pair.get('execution_authority') == 'primary_only'
                 and pair.get('confidence_is_calibrated_probability') is False
                 and pair.get('outcome_status') == 'not_evaluated', 'model_pair_authority')
        observations = {}
        for role, provider in (('primary', 'anthropic'), ('challenger', 'openai')):
            item = pair.get(role)
            if type(item) is not dict or item.get('provider') != provider:
                statuses[role]['unknown'] += 1
                continue
            status = item.get('status')
            known = status if type(status) is str and status in {'completed', 'invalid_schema', 'unavailable', 'budget_exhausted'} else 'unknown'
            statuses[role][known] += 1
            if known != 'completed':
                continue
            observations[role] = item
        if set(observations) != {'primary', 'challenger'}:
            refused['incomplete_pair'] += 1
            continue
        prompt = pair.get('prompt_sha256')
        if (type(prompt) is not str or not SHA.fullmatch(prompt)
                or any(item.get('prompt_sha256') != prompt for item in observations.values())):
            refused['evidence_mismatch_or_unknown'] += 1
            continue
        valid = True
        for item in observations.values():
            try:
                began, ended = (_instant(item.get(k)) for k in ('started_at', 'completed_at'))
                valid = valid and observed <= began <= ended <= now
            except (TypeError, ValueError):
                valid = False
            consensus = item.get('consensus')
            valid = valid and type(consensus) is dict and type(consensus.get('stance')) is str and consensus.get('stance') in {'BUY', 'SELL', 'NEUTRAL'}
        if not valid:
            refused['completion_time_or_stance_unknown'] += 1
            continue
        matched += 1
        agree += observations['primary']['consensus']['stance'] == observations['challenger']['consensus']['stance']
        for role, item in observations.items():
            duration = item.get('duration_ms')
            if type(duration) in {int, float} and 0 <= duration <= 600000 and math.isfinite(duration):
                latencies[role].append(Decimal(str(duration)))
            counts = item.get('usage')
            if (type(counts) is dict and all(type(counts.get(k)) is int and 0 <= counts[k] <= 1000000000
                                            for k in ('input_tokens', 'output_tokens'))):
                usage[role].append((counts['input_tokens'], counts['output_tokens']))
    return {'schema': 'pramana.recorded_model_pair_summary.v1', 'as_of': now.isoformat(),
            'recorded_pairs': len(pairs), 'window_complete': now >= end,
            'completed_same_evidence_pairs': matched, 'stance_agreements': agree,
            'stance_disagreements': matched - agree, 'unusable_pair_reasons': dict(sorted(refused.items())),
            'roles': {role: {'statuses': dict(sorted(statuses[role].items())),
                             'latency_samples': len(latencies[role]),
                             'mean_duration_ms': _mean(latencies[role]),
                             'complete_usage_samples': len(usage[role]),
                             'input_tokens': sum(p[0] for p in usage[role]),
                             'output_tokens': sum(p[1] for p in usage[role])}
                      for role in statuses},
            'provider_calls_made': False, 'execution_authority': 'primary_only',
            'forecast_skill_evaluated': False, 'billed_spend_evaluated': False,
            'source_authenticity_verified': False, 'winner_selected': False,
            'limitations': ['Only supplied recorded pairs are counted; absence does not prove the host comparison flag is off.',
                            'Same declared prompt hash does not authenticate inputs or establish identical model systems.',
                            'Different completion latency can matter; confidence is not calibrated probability.',
                            'Unknown usage/latency stays unknown; usage counts are not a billing reconciliation.',
                            'No realized outcome, counterfactual trade or model update is inferred from stance agreement.']}
