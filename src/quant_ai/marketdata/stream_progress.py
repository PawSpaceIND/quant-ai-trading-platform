"""Bounded receipt/progress counters; no payload, credentials or recovery authority."""
from __future__ import annotations

from collections import Counter
from threading import RLock

from quant_ai.marketdata.tick_integrity import utc_time


class StreamProgress:
    def __init__(self, clock):
        self.clock = clock
        self.lock = RLock()
        self.generation = 0
        self.counts = Counter()
        self.times = {}
        self.pending = self.current_pending = self.current_failures = 0
        self.pending_since = None
        self.subscription_state = 'not_requested'
        self.native_retry_active = False
        self.watchdog_state = 'disabled'

    def start(self):
        with self.lock:
            self.generation += 1
            self.times = {'generationStartedAt': utc_time(self.clock())}
            self.current_pending = self.current_failures = 0
            self.pending_since = None
            self.subscription_state = 'not_requested'
            self.native_retry_active = False

    def note(self, event):
        fields = {'raw': ('rawFrameCallbacks', 'lastRawFrameAt'),
                  'ticks': ('tickCallbacks', 'lastTickCallbackAt'),
                  'connected': ('connectCallbacks', 'lastConnectedAt'),
                  'subscription': ('subscriptionRequests', 'subscriptionRequestAt'),
                  'retry': ('nativeRetryCallbacks', 'nativeRetryAt'),
                  'ignored': ('ignoredOldCallbacks', 'ignoredOldCallbackAt')}
        count, stamp = fields[event]
        with self.lock:
            self.counts[count] += 1
            self.times[stamp] = utc_time(self.clock())

    def subscribed(self, failed=False):
        with self.lock:
            self.subscription_state = 'send_failed' if failed else 'sent_unconfirmed'

    def retrying(self, active):
        with self.lock:
            self.native_retry_active = active

    def watchdog(self, state):
        with self.lock:
            self.watchdog_state = state

    def queued(self):
        with self.lock:
            self.counts['futuresQueued'] += 1
            self.pending += 1
            self.current_pending += 1
            if self.pending_since is None:
                self.pending_since = utc_time(self.clock())
            return self.generation

    def finished(self, generation, *, failed=False, cancelled=False):
        with self.lock:
            self.pending -= 1
            self.counts['futuresCancelled' if cancelled else 'futuresFailed' if failed else 'futuresCompleted'] += 1
            if generation == self.generation:
                self.current_pending -= 1
                if not self.current_pending:
                    self.pending_since = None
                if failed:
                    self.current_failures += 1
                self.times['lastFutureCompletionAt'] = utc_time(self.clock())

    def snapshot(self):
        with self.lock:
            result = {'schema': 'pramana.zerodha_ingestion_progress.v1',
                      'connectionGeneration': self.generation, 'counts': dict(self.counts),
                      'pendingFutures': self.pending, 'currentGenerationPendingFutures': self.current_pending,
                      'currentGenerationFutureFailures': self.current_failures,
                      'pendingSince': self.pending_since.isoformat() if self.pending_since else None,
                      'subscriptionSendState': self.subscription_state,
                      'nativeRetryActive': self.native_retry_active, 'watchdogState': self.watchdog_state,
                      'subscriptionAcknowledged': False,
                      'scope': 'process_counters; receipt_times_current_generation; no_payloads_or_wire_sequence_proof'}
            result.update({key: value.isoformat() for key, value in self.times.items()})
            return result
