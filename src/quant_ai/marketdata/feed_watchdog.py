"""Opt-in bounded recovery classification; never freshness or entry permission."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class FeedRecoveryPolicy:
    silent_seconds: int = 180
    poll_seconds: int = 10

    def __post_init__(self):
        if (type(self.silent_seconds) is not int or not 120 <= self.silent_seconds <= 900
                or type(self.poll_seconds) is not int or not 5 <= self.poll_seconds <= 30):
            raise ValueError('feed_recovery_policy_invalid')


def progress_issue(progress, now, active_since, policy):
    """Classify a single current generation without interpreting raw payloads.

    No broker subscription acknowledgment or loop-wide health is inferred. Pending
    or failed consumers are manual recovery, never an automatic socket replacement.
    """
    try:
        if (progress['schema'] != 'pramana.zerodha_ingestion_progress.v1'
                or type(progress['connectionGeneration']) is not int or progress['connectionGeneration'] < 1
                or now.utcoffset() is None or active_since.utcoffset() is None):
            return 'progress_unknown'
        began = datetime.fromisoformat(progress['generationStartedAt'])
        if began.utcoffset() is None or began > now or active_since > now:
            return 'progress_unknown'
        if now - max(began, active_since) < timedelta(seconds=policy.silent_seconds):
            return 'grace_period'
        if progress['nativeRetryActive'] is True:
            return 'native_retry_active'
        if progress['nativeRetryActive'] is not False:
            return 'progress_unknown'
        for field in ('pendingFutures', 'currentGenerationFutureFailures'):
            if type(progress[field]) is not int or progress[field] < 0:
                return 'progress_unknown'
        if progress['currentGenerationFutureFailures']:
            return 'consumer_future_failed_manual_recovery'
        if progress['pendingFutures']:
            return 'consumer_future_pending_no_recovery'
        if progress['subscriptionSendState'] == 'send_failed':
            return 'subscription_send_failed_manual_recovery'
        if progress['subscriptionSendState'] not in ('not_requested', 'sent_unconfirmed'):
            return 'progress_unknown'
        stamps = {}
        for field in ('lastRawFrameAt', 'lastTickCallbackAt', 'lastConnectedAt'):
            stamps[field] = datetime.fromisoformat(progress[field]) if field in progress else None
            if stamps[field] is not None and (stamps[field].utcoffset() is None or stamps[field] > now):
                return 'progress_unknown'
        if stamps['lastTickCallbackAt'] is not None and now - stamps['lastTickCallbackAt'] < timedelta(seconds=policy.silent_seconds):
            return 'receiving_tick_callbacks'
        if stamps['lastRawFrameAt'] is not None and now - stamps['lastRawFrameAt'] < timedelta(seconds=policy.silent_seconds):
            return 'raw_frames_without_tick_callbacks'
        return 'silent_raw_feed' if stamps['lastConnectedAt'] is not None else 'connection_callback_missing'
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return 'progress_unknown'
