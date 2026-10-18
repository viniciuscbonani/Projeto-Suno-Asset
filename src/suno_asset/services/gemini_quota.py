"""Gemini account ledger: unknown limits stay unknown, independently of Groq."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.quota_ledger import FreeQuotaLedger, _time
from suno_asset.services.llm_budget import seconds


@dataclass(frozen=True)
class GeminiQuotaWindow:
    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    tokens_per_minute: int | None = None
    tokens_per_day: int | None = None

    def __post_init__(self):
        if any(value is not None and (type(value) is not int or value <= 0)
               for value in self.__dict__.values()):
            raise ValueError('limites Gemini configurados devem ser inteiros positivos')


class GeminiQuotaLedger(FreeQuotaLedger):
    def __init__(self, path, window: GeminiQuotaWindow, *, scope: str):
        import fcntl
        path = Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        # SQLite WAL initialization itself can race before busy_timeout applies.
        # Serialize schema creation across threads AND processes, not transports.
        with path.with_suffix('.init.lock').open('a') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
            try:
                self._initialize(path,window,scope=scope)
            finally:
                fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

    def _initialize(self,path,window,*,scope):
        super().__init__(path, window)
        self.scope = scope
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS provider_observations (
                    id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL,
                    provider TEXT NOT NULL, scope TEXT NOT NULL, model TEXT NOT NULL,
                    configuration TEXT NOT NULL, observed TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_cooldown (
                    id INTEGER PRIMARY KEY CHECK (id=1), until_at TEXT,
                    observed_at TEXT NOT NULL, model TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_scope (
                    id INTEGER PRIMARY KEY CHECK (id=1), scope TEXT NOT NULL
                );
            ''')
            db.execute('INSERT OR IGNORE INTO provider_scope VALUES (1,?)', (scope,))
            if db.execute('SELECT scope FROM provider_scope WHERE id=1').fetchone()['scope'] != scope:
                raise ExpectedServiceError('gemini_config_invalid', 'ledger Gemini pertence a outro escopo de conta')

    def reserve_attempt(self, *, now=None, **kwargs):
        instant = now or datetime.now(timezone.utc)
        with self._connect() as db:
            row = db.execute('SELECT * FROM provider_cooldown WHERE id=1').fetchone()
            last = db.execute('SELECT observed_at,observed FROM provider_observations ORDER BY id DESC LIMIT 1').fetchone()
        if row and (row['until_at'] is None or datetime.fromisoformat(row['until_at']) > instant):
            raise ExpectedServiceError('gemini_rate_limit', 'cota Gemini indisponível; reposição persistida ainda não confirmada')
        # Header observations can restrict admission; they never grant quota.
        # Do not reinterpret an unlabeled token limit as a new TPM allocation.
        if last:
            rate = json.loads(last['observed']).get('rate_limits')
            if isinstance(rate, dict):
                for remaining_key, reset_key, needed in (
                    ('remaining', 'reset', kwargs['tokens']),
                    ('requests_remaining', 'requests_reset', 1)):
                    try:
                        remaining = int(rate[remaining_key])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if remaining < needed:
                        reset = seconds(rate.get(reset_key))
                        ready = datetime.fromisoformat(last['observed_at']) + timedelta(seconds=reset + .5) if reset is not None else None
                        if ready is None or instant < ready:
                            raise ExpectedServiceError('gemini_rate_limit', 'saldo observado Gemini insuficiente; reposição ainda não confirmada')
        return super().reserve_attempt(now=instant, **kwargs)

    def observe(self, model: str, observed: dict, *, retry_seconds: float | None = None, rate_limited=False):
        instant = datetime.now(timezone.utc)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO provider_observations VALUES (NULL,?,?,?,?,?,?)',
                (_time(instant), 'gemini', self.scope, model,
                 json.dumps(self.window.__dict__, sort_keys=True), json.dumps(observed, sort_keys=True)))
            if rate_limited:
                until = _time(instant + timedelta(seconds=retry_seconds)) if retry_seconds is not None else None
                previous = db.execute('SELECT until_at FROM provider_cooldown WHERE id=1').fetchone()
                if previous:
                    until = max(previous['until_at'], until) if previous['until_at'] and until else None
                db.execute('INSERT OR REPLACE INTO provider_cooldown VALUES (1,?,?,?)',
                    (until, _time(instant), model))
            db.commit()

    def snapshot(self, **kwargs):
        result = super().snapshot(**kwargs)
        with self._connect() as db:
            cooldown = db.execute('SELECT * FROM provider_cooldown WHERE id=1').fetchone()
            observations = db.execute('SELECT * FROM provider_observations ORDER BY id').fetchall()
        return {**result, 'provider': 'gemini', 'account_scope': self.scope,
            'cooldown': dict(cooldown) if cooldown else None,
            'observations': [{**dict(row), 'configuration': json.loads(row['configuration']),
                'observed': json.loads(row['observed'])} for row in observations]}
