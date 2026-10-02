"""Default-off, passive PAPER forecasts from frozen timestamped quote snapshots.

A reviewed numeric candidate is supplied, never invented or trained here. The
artifact versions its horizon mark rule; unavailable endpoints stay pending.
"""
from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

from quant_ai.learning.forecast_evaluation import _metrics
from quant_ai.learning.outcomes import ForecastOutcomeJournal
from quant_ai.learning.shadow import (
    ENDPOINT_POLICY,
    INPUT_SCHEMA,
    MODEL_SCHEMA_V2,
    ShadowForecastWriter,
    ShadowModelBundle,
    _canonical,
    _check,
    _decimal,
    _hash,
    _identifier,
    _instant,
    _private_existing,
    decode,
    validate_shadow_lineage,
)
from quant_ai.marketdata.tick_integrity import tick_value_issue
from quant_ai.marketdata.ticker_stream import TickBuffer

SCHEMA = 'pramana.prospective_shadow_plan.v1'
QUOTE_FIELDS = {'market_last_price': 'last_price', 'market_volume': 'volume',
                'market_bid': 'bid', 'market_ask': 'ask', 'market_spread_bps': 'spread_bps'}
MAX_BATCH = 50


def validate_plan(raw, reviewed_sha256):
    _check(_hash(decode(raw)) == reviewed_sha256, 'reviewed_plan_digest')
    plan = decode(raw)
    _check(type(plan) is dict and set(plan) == {'schema', 'frozen_at', 'bundle', 'cost_return'}
           and plan['schema'] == SCHEMA, 'prospective_plan_schema')
    bundle = ShadowModelBundle.from_payload(plan['bundle'])
    model = bundle.validate()
    _check(model['schema'] == MODEL_SCHEMA_V2 and model['endpoint_policy_id'] == ENDPOINT_POLICY,
           'versioned_endpoint_policy_required')
    frozen = _instant(plan['frozen_at'])
    _check(bundle.training_run.trained_at <= frozen, 'plan_precedes_training')
    _check(set(model['feature_names']) <= QUOTE_FIELDS.keys(), 'timestamped_quote_features_only')
    cost = _decimal(plan['cost_return'])
    _check(0 <= cost <= 1 and model['cost_policy_id'] == 'fixed_shadow_round_trip.v1'
           and model['cost_policy_sha256'] == _hash({'id':model['cost_policy_id'],
               'cost_return':plan['cost_return']}), 'frozen_cost_policy')
    return plan, bundle


class ProspectiveShadowObserver:
    """One bounded worker; no broker handle, transport, fitter or trading authority."""

    def __init__(self, raw_plan, *, reviewed_sha256, journal_path, tenant_id, buffer,
                 paper_only, clock=None):
        _check(paper_only is True and type(buffer) is TickBuffer, 'passive_paper_buffer_required')
        self.plan, self.bundle = validate_plan(raw_plan, reviewed_sha256)
        self.plan_sha = reviewed_sha256
        _check(str(journal_path) != ':memory:', 'durable_shadow_path_required')
        self.path, self.tenant = Path(journal_path), _identifier(tenant_id)
        self.buffer = buffer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='paper-shadow')
        self._future, self._closed = None, False
        self._lock = RLock()
        self._dropped, self._failures = 0, 0
        self.last_report = None

    def submit(self, requests):
        """Never await disk/inference on the ingestion loop; caller cannot reuse inputs."""
        with self._lock:
            if self._closed:
                return 'closed'
            if self._future is not None and not self._future.done():
                self._dropped += len(requests)
                return 'worker_busy'
            _check(type(requests) in (list, tuple) and len(requests) <= MAX_BATCH, 'capture_batch_bound')
            # Strict bounded canonical copy rejects non-JSON/future mutable objects.
            copied = [decode(_canonical(request).encode()) for request in requests]
            self._future = self._pool.submit(self.process, copied)
            self._future.add_done_callback(self._complete)
            return 'submitted'

    def _complete(self, future):
        with self._lock:
            try:
                self.last_report = future.result()
            except Exception:  # noqa: BLE001 - bounded evidence failure, no trading effect
                self._failures += 1

    def close(self):
        with self._lock:
            self._closed = True
            self._pool.shutdown(wait=False, cancel_futures=True)

    def status(self):
        with self._lock:
            return {'enabled': True, 'worker_busy': self._future is not None and not self._future.done(),
                    'dropped_capture_count_since_start':self._dropped,
                    'failed_batches_since_start':self._failures,
                    'complete_capture_verified':False, 'trading_authorized':False}

    def _input(self, request, now):
        _check(type(request) is dict and set(request) == {'decision_id','proof_decision_id','snapshot'}, 'capture_schema')
        pair = _identifier(request['decision_id'])
        _identifier(request['proof_decision_id'])
        snap = request['snapshot']
        _check(type(snap) is dict and snap.get('schema') == 'pramana.feature_snapshot.v1'
               and snap.get('metrics_truncated') is False, 'decision_snapshot_required')
        frozen = _instant(snap.get('frozen_at'))
        _check(_instant(self.plan['frozen_at']) < frozen <= now
               and now - frozen <= timedelta(seconds=120), 'prospective_capture_time')
        market = snap.get('market')
        _check(type(market) is dict and market.get('source') == 'zerodha', 'quote_source_required')
        observed = _instant(market.get('observed_at'))
        _check(observed <= frozen and frozen - observed <= timedelta(seconds=60), 'quote_cutoff')
        _check('zerodha' in self.bundle.dataset.source_ids, 'candidate_source_binding')
        price = _decimal(market.get('last_price'))
        for field in ('volume','bid','ask'):
            if market.get(field) is not None:
                _check(_decimal(market[field]) >= 0, 'quote_numeric_geometry')
        if market.get('bid') is not None and market.get('ask') is not None:
            _check(_decimal(market['ask']) >= _decimal(market['bid']), 'quote_crossed')
        _check(price > 0, 'positive_reference_price')
        values = {name:market.get(QUOTE_FIELDS[name]) for name in self.bundle.validate()['feature_names']}
        for value in values.values():
            _decimal(value)
        vector = {'schema':INPUT_SCHEMA, 'subject':_identifier(snap.get('subject')),
                  'observed_at':observed.isoformat(), 'available_at':now.isoformat(),
                  'source_ids':['zerodha'], 'values':values}
        return pair, vector, price

    def process(self, requests):
        """Worker-only operation; writes a separate private SHADOW journal."""
        _check(type(requests) in (list,tuple) and len(requests)<=MAX_BATCH,'capture_batch_bound')
        requests = [decode(_canonical(request).encode()) for request in requests]
        _check(_hash(self.plan) == self.plan_sha and _hash(self.bundle.payload()) == _hash(self.plan['bundle']),
               'frozen_plan_changed')
        with ShadowForecastWriter(self.path, tenant_id=self.tenant, clock=self.clock) as writer:
            db = writer.journal.db
            db.execute('PRAGMA busy_timeout=250')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            _check('prospective_plan' in tables or db.execute('SELECT COUNT(*) FROM probability_forecasts').fetchone()[0] == 0,
                'dedicated_prospective_journal_required')
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS prospective_plan(singleton INTEGER PRIMARY KEY CHECK(singleton=1),sha256 TEXT NOT NULL)')
                db.execute('INSERT OR IGNORE INTO prospective_plan VALUES(1,?)', (self.plan_sha,))
                _check(db.execute('SELECT sha256 FROM prospective_plan').fetchone()[0] == self.plan_sha,
                       'journal_plan_reused')
                db.execute('CREATE TABLE IF NOT EXISTS prospective_captures(pair_id TEXT PRIMARY KEY,input_sha256 TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,forecast_id TEXT,reference_price TEXT,recorded_at TEXT NOT NULL,proof_decision_id TEXT,opportunity_key TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS prospective_unlinked_refusals(input_sha256 TEXT PRIMARY KEY,reason TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS prospective_endpoints(forecast_id TEXT PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
                # Mutable scheduling metadata, separate from append-only evidence.
                db.execute('CREATE TABLE IF NOT EXISTS prospective_resolution_cursor(singleton INTEGER PRIMARY KEY CHECK(singleton=1),last_rowid INTEGER NOT NULL)')
                db.execute('INSERT OR IGNORE INTO prospective_resolution_cursor VALUES(1,0)')
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS prospective_unique_opportunity ON prospective_captures(opportunity_key) WHERE status='recorded'")
                for table in ('prospective_plan','prospective_captures','prospective_endpoints','prospective_unlinked_refusals'):
                    for verb in ('UPDATE','DELETE'):
                        db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()}_blocked BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Prospective evidence is append-only'); END")
            for request in requests:
                now = _instant(self.clock())
                pair = request.get('decision_id') if type(request) is dict else None
                try:
                    _identifier(pair)
                except ValueError:
                    with db:
                        digest = _hash(request)
                        exists = db.execute('SELECT 1 FROM prospective_unlinked_refusals WHERE input_sha256=?', (digest,)).fetchone()
                        if not exists:
                            row = db.execute('INSERT OR IGNORE INTO prospective_unlinked_refusals SELECT ?,? WHERE (SELECT COUNT(*) FROM prospective_captures)+(SELECT COUNT(*) FROM prospective_unlinked_refusals)<10000',
                                (digest,'missing_or_invalid_decision_id'))
                            _check(row.rowcount == 1,'prospective_attempt_bound')
                    continue  # no fabricated decision/proof ID
                digest = _hash(request)
                old = db.execute('SELECT * FROM prospective_captures WHERE pair_id=?', (pair,)).fetchone()
                if old:
                    _check(old['input_sha256'] == digest, 'decision_input_reused')
                    continue
                try:
                    _check(db.execute('SELECT COUNT(*) FROM probability_forecasts WHERE pair_id=? AND candidate_id=?',
                        (pair,self.bundle.training_run.candidate_id)).fetchone()[0] == 0,'unlinked_existing_forecast')
                    # Exchange event time is not receipt time. Availability is the actual
                    # worker capture instant, never invented at the older snapshot cutoff.
                    _, vector, price = self._input(request, now)
                    opportunity_key = _hash([vector['subject'],request['snapshot']['frozen_at']])
                    _check(db.execute("SELECT COUNT(*) FROM prospective_captures WHERE opportunity_key=? AND status='recorded'",
                        (opportunity_key,)).fetchone()[0] == 0,'duplicate_opportunity')
                    fields = (pair,digest,str(price),now.isoformat(),request.get('proof_decision_id'),opportunity_key)
                    writer.record(self.bundle, vector, pair_id=pair,
                        commit_hook=lambda connection,item,fields=fields: self._persist_capture(
                            connection,fields[0],fields[1],'recorded',None,item.forecast_id,*fields[2:]))
                except (ValueError, TypeError, ArithmeticError, KeyError):
                    with db:
                        self._persist_capture(db,pair,digest,'refused','input_or_candidate_unqualified',
                            None,None,now.isoformat(),request.get('proof_decision_id'),None)
            self._resolve(writer)
            return self.report(writer)

    @staticmethod
    def _persist_capture(db,*values):
        row = db.execute('INSERT INTO prospective_captures SELECT ?,?,?,?,?,?,?,?,? WHERE (SELECT COUNT(*) FROM prospective_captures)+(SELECT COUNT(*) FROM prospective_unlinked_refusals)<10000',values)
        _check(row.rowcount == 1,'prospective_attempt_bound')

    def _resolve(self, writer):
        db, now = writer.journal.db, _instant(self.clock())
        cursor = db.execute('SELECT last_rowid FROM prospective_resolution_cursor WHERE singleton=1').fetchone()[0]
        rows = db.execute('''SELECT f.*,f.rowid AS traversal_id,c.reference_price FROM probability_forecasts f
            JOIN prospective_captures c USING(forecast_id)
            LEFT JOIN forecast_outcomes o USING(forecast_id)
            WHERE o.forecast_id IS NULL AND f.resolve_after<=?
            ORDER BY (f.rowid<=?),f.rowid LIMIT 64''',
            (now.isoformat(),cursor)).fetchall()
        if rows:
            # Advance durably before attempting this bounded page. A failed/crashed
            # page is retried on wrap, but cannot monopolize every subsequent cycle.
            # Unknown outcomes remain pending; scheduling never fabricates labels.
            with db:
                db.execute('UPDATE prospective_resolution_cursor SET last_rowid=? WHERE singleton=1',
                           (rows[-1]['traversal_id'],))
        for row in rows:
            due = _instant(row['resolve_after'])
            existing = db.execute('SELECT payload,sha256 FROM prospective_endpoints WHERE forecast_id=?',
                                  (row['forecast_id'],)).fetchone()
            if existing:
                evidence = decode(existing['payload'].encode())
                _check(_hash(evidence) == existing['sha256'], 'endpoint_digest')
            else:
                tick = self.buffer.latest_at_or_before(row['subject'], due)
                if (tick is None or tick_value_issue(tick) or tick.source != 'zerodha'
                        or tick.symbol != row['subject'] or _instant(tick.observed_at) <= _instant(row['decision_at'])
                        or not timedelta(0) <= due-_instant(tick.observed_at) <= timedelta(
                            seconds=self.bundle.validate()['maximum_endpoint_age_seconds'])):
                    continue  # missing/stale/late-only endpoint remains pending, never zero
                evidence = {'forecast_id':row['forecast_id'],'subject':row['subject'],
                    'observed_at':_instant(tick.observed_at).isoformat(),'available_at':now.isoformat(),
                    'price':str(tick.ltp),'reference_price':row['reference_price'],
                    'cost_return':self.plan['cost_return'],'plan_sha256':self.plan_sha}
                # Durable evidence first: crash before settlement can replay the exact witness.
                with db:
                    db.execute('INSERT INTO prospective_endpoints VALUES(?,?,?)',
                        (row['forecast_id'],_canonical(evidence),_hash(evidence)))
            _check(evidence['forecast_id'] == row['forecast_id'] and evidence['subject'] == row['subject']
                   and _instant(row['decision_at']) < _instant(evidence['observed_at'])
                   and timedelta(0) <= due-_instant(evidence['observed_at']) <= timedelta(seconds=self.bundle.validate()['maximum_endpoint_age_seconds'])
                   and due <= _instant(evidence['available_at']) <= now
                   and evidence['reference_price'] == row['reference_price']
                   and evidence['plan_sha256'] == self.plan_sha
                   and evidence['cost_return'] == self.plan['cost_return'], 'endpoint_binding')
            price, reference = _decimal(evidence['price']), _decimal(evidence['reference_price'])
            _check(price > 0 and reference > 0, 'endpoint_price')
            with localcontext(Context(prec=96, rounding=ROUND_HALF_EVEN)):
                cost = _decimal(evidence['cost_return'])
                gross = self._gross_return(price,reference,cost)
                writer.journal.resolve(row['forecast_id'], gross_return=gross,
                    cost_return=cost, resolved_at=_instant(evidence['available_at']))

    @staticmethod
    def _gross_return(price,reference,cost):
        _check(price>0 and reference>0,'endpoint_price')
        delta = price-reference
        gross = (delta/reference).quantize(Decimal('1e-24'))
        _check((delta-reference*cost>0) == (gross-cost>0),'rounding_changes_label')
        return gross

    def report(self, writer):
        validate_shadow_lineage(writer.journal.db, tenant_id=self.tenant)
        db = writer.journal.db
        counts = dict(db.execute('SELECT status,COUNT(*) FROM prospective_captures GROUP BY status').fetchall())
        rows = writer.journal._resolved_rows(self.bundle.training_run.candidate_id)
        _check(all(row['candidate_id'] == self.bundle.training_run.candidate_id for row in rows), 'candidate_report_binding')
        # A resolved label must retain its exact endpoint witness; never silently score
        # an independently inserted outcome or a changed cost/price.
        for row in rows:
            endpoint = db.execute('SELECT payload,sha256 FROM prospective_endpoints WHERE forecast_id=?',
                (row['forecast_id'],)).fetchone()
            capture = db.execute('SELECT reference_price FROM prospective_captures WHERE forecast_id=?',
                (row['forecast_id'],)).fetchone()
            _check(endpoint is not None and capture is not None, 'resolved_witness_missing')
            evidence = decode(endpoint['payload'].encode())
            _check(_hash(evidence) == endpoint['sha256'] and evidence['forecast_id'] == row['forecast_id']
                and evidence['subject'] == row['subject']
                and _instant(row['decision_at']) < _instant(evidence['observed_at'])
                and _instant(row['resolved_at']) <= _instant(self.clock())
                and timedelta(0) <= _instant(row['resolve_after'])-_instant(evidence['observed_at']) <= timedelta(seconds=self.bundle.validate()['maximum_endpoint_age_seconds'])
                and evidence['available_at'] == row['resolved_at']
                and evidence['reference_price'] == capture['reference_price']
                and evidence['plan_sha256'] == self.plan_sha and evidence['cost_return'] == self.plan['cost_return'],
                'resolved_witness_binding')
            reference, price = _decimal(evidence['reference_price']), _decimal(evidence['price'])
            with localcontext(Context(prec=96, rounding=ROUND_HALF_EVEN)):
                cost = _decimal(evidence['cost_return'])
                gross = self._gross_return(price,reference,cost)
                _check(gross==Decimal(row['gross_return'])
                    and cost==Decimal(row['cost_return']) and gross-cost==Decimal(row['after_cost_return'])
                    and int(gross-cost>0)==row['positive_after_cost'], 'resolved_label_binding')
        candidate = baseline = None
        if rows:
            probabilities = [Decimal(row['probability']) for row in rows]
            targets = [Decimal(row['positive_after_cost']) for row in rows]
            candidate = _metrics(probabilities, targets)
            baseline = _metrics([Decimal('0.5')]*len(rows), targets)
        return {'schema':'pramana.prospective_shadow_evaluation.v1','plan_sha256':self.plan_sha,
            'capture_status_counts':counts, 'resolved':len(rows),
            'pending':counts.get('recorded',0)-len(rows),
            'unlinked_refused_inputs':db.execute('SELECT COUNT(*) FROM prospective_unlinked_refusals').fetchone()[0],
            'unlinked_forecasts':db.execute('SELECT COUNT(*) FROM probability_forecasts f LEFT JOIN prospective_captures c USING(forecast_id) WHERE c.forecast_id IS NULL').fetchone()[0], 'candidate':candidate,
            'constant_half_baseline':baseline,'descriptive_sample_count_met':len(rows)>=30,
            'cost_basis':'declared_frozen_fraction; not measured execution costs',
            'calibration_verified':False,'source_authenticity_verified':False,
            'out_of_sample_skill_verified':False,'complete_capture_verified':False,
            'trading_authorized':False,'model_promoted':False}


def observer_from_env(*, buffer, tenant_id, paper_only, environ=None):
    env = os.environ if environ is None else environ
    enabled = env.get('PRAMANA_PROSPECTIVE_SHADOW_ENABLED', 'false')
    if enabled == 'false':
        return None
    _check(enabled == 'true' and paper_only is True
           and env.get('TRADING_LIVE_MONEY_ACTIVE','false') == 'false', 'passive_paper_gate')
    path = Path(env['PRAMANA_PROSPECTIVE_SHADOW_PLAN'])
    _private_existing(path)
    with path.open('rb') as handle:
        raw = handle.read(65537)
    return ProspectiveShadowObserver(raw, reviewed_sha256=env['PRAMANA_PROSPECTIVE_SHADOW_PLAN_SHA256'],
        journal_path=env['PRAMANA_PROSPECTIVE_SHADOW_DB'],tenant_id=tenant_id,
        buffer=buffer,paper_only=paper_only)


def read_evaluation(path, *, raw_plan, reviewed_sha256, tenant_id, now=None):
    """Constructor-free WAL-aware read of one frozen plan's existing private journal."""
    path = Path(path)
    _private_existing(path)
    # Read-only view objects deliberately bypass schema-writing constructors.
    reader = object.__new__(ProspectiveShadowObserver)
    reader.plan, reader.bundle = validate_plan(raw_plan,reviewed_sha256)
    reader.plan_sha, reader.tenant = reviewed_sha256, _identifier(tenant_id)
    moment = _instant(now or datetime.now(timezone.utc))
    reader.clock = lambda: moment
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        _check(db.execute('SELECT sha256 FROM prospective_plan').fetchone()[0] == reviewed_sha256,
               'journal_plan_reused')
        journal = object.__new__(ForecastOutcomeJournal)
        journal.db = db
        return reader.report(SimpleNamespace(journal=journal))
