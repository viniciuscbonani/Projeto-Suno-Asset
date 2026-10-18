"""A process restart must not turn short persisted quota waits into attempts."""
from datetime import datetime, timedelta, timezone

import pytest

from suno_asset.services import groq
from suno_asset.services.groq import EXTRACT_SCHEMA, GroqSession
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.quota_ledger import QuotaWindow


class Clock:
    now = 100.0

    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        assert 0 <= seconds <= 10
        self.sleeps.append(seconds)
        self.now += seconds


class Ledger:
    window = QuotaWindow()

    def __init__(self, reason, wait=20):
        self.reservations = 0
        self.settlements = []
        self.next_window = {'reason': reason, 'available_at':
            (datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat()}

    def reserve_attempt(self, **kwargs):
        self.reservations += 1
        if self.reservations == 1:
            raise ExpectedServiceError('free_quota_deferred', 'synthetic persisted window')
        return 'only-provider-attempt'

    def next_available_window(self, job_id):
        return self.next_window

    def settle_attempt(self, attempt_id, **kwargs):
        self.settlements.append((attempt_id, kwargs))


def setup(monkeypatch, reason, deadline=100):
    clock = Clock()
    monkeypatch.setattr(groq.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(groq.time, 'sleep', clock.sleep)
    ledger = Ledger(reason)
    fixed_utc = datetime.now(timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc

    monkeypatch.setattr(groq, 'datetime', FrozenDatetime)
    ledger.next_window['available_at'] = (fixed_utc + timedelta(seconds=20)).isoformat()
    seen = []

    def transport(payload, key, timeout):
        seen.append((clock.now, timeout))
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}

    session = GroqSession('synthetic-key', transport=transport, started=clock.now,
        free_only=True, quota_evidence={'synthetic': True}, quota_ledger=ledger,
        quota_job_id='same-immutable-job', deadline_seconds=deadline)
    return session, ledger, clock, seen


@pytest.mark.parametrize('reason', ['TPM', 'RPM', 'TPM + RPM'])
def test_persisted_minute_wait_does_not_consume_provider_attempt(monkeypatch, reason):
    session, ledger, clock, seen = setup(monkeypatch, reason, deadline=30)
    assert session.call('extract', 'system', {}, EXTRACT_SCHEMA, 100) == {}
    assert ledger.reservations == 2 and len(ledger.settlements) == 1
    assert len(seen) == session.calls == len(session.attempt_records) == 1
    assert session.attempt_records[0]['status'] == 'complete'
    assert 20 <= sum(clock.sleeps) <= 20.2
    assert session.started == 100
    assert session.reserved_tokens == 15
    assert session.rate_gate.reservations == [(clock.now, 15)]
    assert 9.8 <= seen[0][1] <= 10


@pytest.mark.parametrize('reason', ['TPD', 'RPD', 'TPM + TPD', 'RPM + RPD'])
def test_daily_window_defers_without_waiting_or_provider_attempt(monkeypatch, reason):
    session, ledger, clock, seen = setup(monkeypatch, reason)
    with pytest.raises(ExpectedServiceError, match='synthetic persisted window') as error:
        session.call('extract', 'system', {}, EXTRACT_SCHEMA, 100)
    assert error.value.code == 'free_quota_deferred'
    assert not clock.sleeps and not seen
    assert ledger.reservations == 1 and not ledger.settlements
    assert session.calls == session.reserved_tokens == 0
    assert session.rate_gate.reservations == []
    assert session.attempt_records[0]['status'] == 'deferred'


def test_persisted_minute_wait_respects_original_deadline(monkeypatch):
    session, ledger, clock, seen = setup(monkeypatch, 'TPM', deadline=10)
    with pytest.raises(ExpectedServiceError) as error:
        session.call('extract', 'system', {}, EXTRACT_SCHEMA, 100)
    assert error.value.code == 'llm_deadline'
    assert not clock.sleeps and not seen and ledger.reservations == 1
    assert session.calls == session.reserved_tokens == 0 and session.started == 100
