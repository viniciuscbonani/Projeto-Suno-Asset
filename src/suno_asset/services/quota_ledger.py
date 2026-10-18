"""Persistent conservative accounting for a shared free-provider quota.

The ledger stores metadata and token counts only. It never stores API keys or
request/response bodies. Unsettled attempts retain their full reservation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from suno_asset.services.protocols import ExpectedServiceError


def _time(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class QuotaWindow:
    requests_per_minute: int = 30
    requests_per_day: int = 1000
    tokens_per_minute: int = 8000
    tokens_per_day: int = 200000

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in (
            self.requests_per_minute, self.requests_per_day,
            self.tokens_per_minute, self.tokens_per_day,
        )):
            raise ValueError('Cotas gratuitas precisam ser inteiros positivos')


def validate_free_quota_evidence(path: Path, expected_models: set[str], window: QuotaWindow,
                                 credential_sha256: str | None = None):
    """Require fresh account-level evidence before the free-only path can call."""
    path = Path(path)
    try:
        evidence = json.loads(path.read_text(encoding='utf-8'))
        attachment = Path(evidence['evidence_file'])
        if not attachment.is_absolute():
            attachment = path.parent / attachment
        checked_at = datetime.fromisoformat(evidence['checked_at'])
        attachment_hash = sha256(attachment.read_bytes()).hexdigest()
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ExpectedServiceError('free_quota_unverified', 'evidência atual da cota gratuita ausente/inválida') from exc
    account_hash=evidence.get('account_id_sha256')
    account_hash_valid=(isinstance(account_hash,str) and len(account_hash)==64
        and all(c in '0123456789abcdef' for c in account_hash.lower()))
    credential_hash=evidence.get('credential_sha256')
    credential_hash_valid=(isinstance(credential_hash,str) and len(credential_hash)==64
        and all(c in '0123456789abcdef' for c in credential_hash.lower())
        and credential_sha256 is not None and credential_hash.lower()==credential_sha256.lower())
    if (evidence.get('version') != 'm7-groq-free-quota-evidence-v1'
        or evidence.get('provider') != 'groq' or evidence.get('account_plan') != 'free'
        or evidence.get('zero_billing_confirmed') is not True
        or not (account_hash_valid or credential_hash_valid)
        or not isinstance(evidence.get('models_available'), list)
        or not expected_models <= set(evidence['models_available'])
        or evidence.get('evidence_sha256') != attachment_hash
        or checked_at.utcoffset() is None
        or datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc) > timedelta(hours=24)
        or checked_at.astimezone(timezone.utc) - datetime.now(timezone.utc) > timedelta(minutes=5)):
        raise ExpectedServiceError('free_quota_unverified', 'plano, modelos ou comprovação de cota gratuita não confirmados nas últimas 24 horas')
    try:
        observed = QuotaWindow(**evidence['limits'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExpectedServiceError('free_quota_unverified', 'limites observados da conta estão ausentes/inválidos') from exc
    if any(getattr(observed, field) > getattr(window, field) for field in window.__dict__):
        raise ExpectedServiceError('free_quota_unverified', 'limites observados excedem os limites gratuitos conservadores')
    return {'evidence_sha256': sha256(path.read_bytes()).hexdigest(),
        'attachment_sha256': attachment_hash, 'checked_at': checked_at.isoformat(),
        'binding': 'credential_sha256' if credential_hash_valid and not account_hash_valid else 'account_id_sha256',
        'limits': observed.__dict__}


class FreeQuotaLedger:
    """SQLite ledger with cross-process atomic reservations and resumable jobs."""

    def __init__(self, path: Path, window: QuotaWindow = QuotaWindow(), *, enforce_tokens_per_day: bool = True):
        self.path = Path(path)
        self.window = window
        # The provider remains authoritative for its daily token quota. The
        # operator may disable only this local TPD admission gate; usage is
        # still ledgered and RPM/RPD/TPM controls remain enforced.
        self.enforce_tokens_per_day = enforce_tokens_per_day
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, model TEXT NOT NULL,
                    job_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                    reserved_tokens INTEGER NOT NULL, actual_tokens INTEGER,
                    status TEXT NOT NULL, http_status INTEGER
                );
                CREATE INDEX IF NOT EXISTS attempts_created ON attempts(created_at);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    source_id TEXT NOT NULL, configuration_id TEXT NOT NULL,
                    status TEXT NOT NULL, result_path TEXT, result_sha256 TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deferred_windows (
                    job_id TEXT PRIMARY KEY, available_at TEXT NOT NULL,
                    reason TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_recoveries (
                    predecessor_id TEXT PRIMARY KEY, successor_id TEXT NOT NULL UNIQUE,
                    reason TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db

    def register_job(self, job_id: str, fingerprint: str, source_id: str,
                     configuration_id: str):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            expected = (fingerprint, source_id, configuration_id)
            if old and (old['fingerprint'], old['source_id'], old['configuration_id']) != expected:
                db.rollback()
                raise ValueError('Tarefa já registrada para outro fingerprint/fonte/configuração')
            if not old:
                db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)',
                    (job_id, *expected, 'queued', None, None, _time()))
            db.commit()

    def job(self, job_id: str):
        with self._connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _read_hashed(path, expected):
        path = Path(path or '')
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Evidência de encerramento ausente ou hash divergente')
        return json.loads(path.read_text(encoding='utf-8'))

    @classmethod
    def _closed_job_evidence(cls, db, job, *, recorded_attempts=None):
        """Prove terminal failure and reconcile every call, including old calls.

        A clock rollover is never evidence of termination or known usage. The
        original result, request/response checkpoints and reservations remain
        unchanged; a recovery creates a distinct execution, never a replay.
        """
        if job is None or job['status'] != 'incomplete':
            raise ValueError('Recuperação exige tarefa encerrada como incomplete')
        row = cls._read_hashed(job['result_path'], job['result_sha256'])
        identity = json.loads(job['configuration_id'])
        if (row.get('split') == 'heldout' or identity.get('source_split') == 'heldout'
            or identity.get('execution_protocol', {}).get('purpose') == 'reserve-matrix'):
            raise ValueError('Reserva iniciada não admite execução sucessora')
        if (row.get('quota_job_id') != job['job_id'] or row.get('quota_status') != 'incomplete'
            or row.get('quota_fingerprint_sha256') != job['fingerprint']
            or row.get('source_id') != job['source_id']):
            raise ValueError('Encerramento diverge da identidade persistente')
        if 'quota_identity' in row:
            if json.dumps(row['quota_identity'], ensure_ascii=False, sort_keys=True) != job['configuration_id']:
                raise ValueError('Identidade da tarefa encerrada divergente')
        attempts = row.get('attempts')
        files = {job['result_path']: job['result_sha256']}
        if type(attempts) is int:
            result = cls._read_hashed(row.get('result_file'), row.get('result_sha256'))
            if result.get('status') != row.get('status'):
                raise ValueError('Status terminal diverge do resultado')
            budget = cls._read_hashed(row.get('budget_file'), row.get('budget_sha256'))
            attempts = budget.get('attempts')
            files.update({str(Path(row['result_file']).resolve()): row['result_sha256'],
                str(Path(row['budget_file']).resolve()): row['budget_sha256']})
        if not isinstance(attempts, list):
            raise ValueError('Encerramento sem trilha de tentativas')
        # Deferral diagnostics are created before transport and have no ledger
        # reservation. They must not erase earlier, actually transmitted calls.
        attempts = [a for a in attempts if a.get('status') != 'deferred']
        ledger_attempts = recorded_attempts if recorded_attempts is not None else db.execute(
            'SELECT * FROM attempts WHERE job_id=? ORDER BY created_at,rowid', (job['job_id'],)).fetchall()
        if len(attempts) != len(ledger_attempts):
            raise ValueError('Tentativas encerradas não reconciliam com o ledger')
        if any(a['status'] != 'reported' for a in ledger_attempts):
            raise ValueError('Consumo ou chamada incerta impede recuperação')
        if attempts:
            from suno_asset.m7_acceptance import validate_trace_hashes
            from suno_asset.services.groq import _reported_usage
            trace = {**row, 'attempts': len(attempts)}
            files.update(validate_trace_hashes(trace))
            audit_dir = Path(row['audit_dir'])
            for index, (attempt, reservation) in enumerate(zip(attempts, ledger_attempts), 1):
                start = json.loads((audit_dir / f'attempt-{index:03}-started.json').read_text())['attempt']
                received = audit_dir / f'attempt-{index:03}-received.json'
                if (not received.is_file() or attempt.get('status') in
                    {'started', 'deferred', 'http_error', 'unavailable'}):
                    raise ValueError('Chamada sem resposta terminal impede recuperação')
                response = json.loads(received.read_text()).get('response')
                usage = _reported_usage(response.get('usage') if isinstance(response, dict) else None)
                if (usage is None or attempt.get('usage_known') is not True
                    or _reported_usage(attempt) != usage or reservation['actual_tokens'] != sum(usage)
                    or attempt.get('attempt') != index
                    or any(attempt.get(key) != start.get(key) for key in
                        ('attempt', 'stage', 'model', 'payload_sha256'))
                    or any(attempt.get(key) != reservation[key] for key in ('model', 'payload_sha256'))
                    or type(attempt.get('input_estimate')) is not int
                    or type(attempt.get('output_reserved')) is not int
                    or reservation['reserved_tokens'] != attempt['input_estimate'] + attempt['output_reserved']):
                    raise ValueError('Uso, reserva ou identidade diverge da resposta terminal')
        elif row.get('audit_hashes'):
            raise ValueError('Auditoria presente sem chamadas reconciliadas')
        from suno_asset.m7_eval import PRICES
        nominal = sum((a['prompt_tokens'] * PRICES[a['model']][0]
            + a['completion_tokens'] * PRICES[a['model']][1]) / 1e6 for a in attempts)
        return {'version': 'm7-job-recovery-evidence-v1', 'files': files, 'job': dict(job),
            'attempts': [dict(a) for a in ledger_attempts], 'result_sha256': job['result_sha256'],
            'nominal_usd': nominal,
            'prompt_tokens': sum(a['prompt_tokens'] for a in attempts),
            'completion_tokens': sum(a['completion_tokens'] for a in attempts)}

    def create_successor(self, job_id: str, *, reason: str):
        """Register one audited successor without changing the failed execution.

        Callers explicitly select recovery after diagnosis. Running, unknown,
        altered or successful jobs cannot be recovered. Registration is atomic
        across workers and the exact reason is part of its immutable evidence.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Recuperação exige justificativa explícita')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            evidence = self._closed_job_evidence(db, job)
            encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
            old = db.execute('SELECT * FROM job_recoveries WHERE predecessor_id=?', (job_id,)).fetchone()
            if old:
                if old['reason'] != reason.strip() or old['evidence_json'] != encoded:
                    raise ValueError('Recuperação já registrada com justificativa/evidência diferente')
                db.commit()
                return dict(db.execute('SELECT * FROM jobs WHERE job_id=?', (old['successor_id'],)).fetchone())
            successor_id = 'recovery-' + sha256(('m7-recovery-v1:' + job_id + ':' + job['result_sha256']).encode()).hexdigest()
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)',
                (successor_id, job['fingerprint'], job['source_id'], job['configuration_id'],
                 'queued', None, None, _time()))
            db.execute('INSERT INTO job_recoveries VALUES (?,?,?,?,?)',
                (job_id, successor_id, reason.strip(), encoded, _time()))
            db.commit()
            return dict(db.execute('SELECT * FROM jobs WHERE job_id=?', (successor_id,)).fetchone())

    def successor_job(self, job_id: str):
        """Read an existing recovery only while its original proof is intact."""
        with self._connect() as db:
            recovery = db.execute('SELECT * FROM job_recoveries WHERE predecessor_id=?', (job_id,)).fetchone()
            if recovery is None:
                return None
            job = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            evidence = self._closed_job_evidence(db, job)
            if json.dumps(evidence, ensure_ascii=False, sort_keys=True) != recovery['evidence_json']:
                raise ValueError('Evidência da recuperação alterada')
            return dict(db.execute('SELECT * FROM jobs WHERE job_id=?', (recovery['successor_id'],)).fetchone())

    def recovery_lineage(self, job_id: str):
        """Immutable predecessor/reason/evidence records, root first."""
        with self._connect() as db:
            rows = []
            while True:
                row = db.execute('SELECT * FROM job_recoveries WHERE successor_id=?', (job_id,)).fetchone()
                if row is None:
                    break
                rows.append(dict(row))
                job_id = row['predecessor_id']
            return list(reversed(rows))

    def resolve_job(self, job_id: str):
        """Follow only explicitly authorized recoveries; never create one."""
        job = self.job(job_id)
        while job is not None:
            successor = self.successor_job(job['job_id'])
            if successor is None:
                return job
            job = successor
        return None

    def pending_jobs(self, fingerprint: str):
        with self._connect() as db:
            rows = db.execute('SELECT * FROM jobs WHERE fingerprint=? AND status IN (?,?) ORDER BY job_id',
                              (fingerprint, 'queued', 'running')).fetchall()
            return [dict(row) for row in rows]

    def next_available_window(self, job_id: str):
        """Return the latest persisted quota deferral for an unstarted job."""
        with self._connect() as db:
            row=db.execute('SELECT * FROM deferred_windows WHERE job_id=?',(job_id,)).fetchone()
            return dict(row) if row else None

    def start_job(self, job_id: str):
        with self._connect() as db:
            changed = db.execute('UPDATE jobs SET status=?,updated_at=? WHERE job_id=? AND status=?',
                ('running', _time(), job_id, 'queued')).rowcount
            if changed != 1:
                raise ValueError('Tarefa já iniciada/concluída; não repetir combinações com chamadas incertas')

    def defer_job(self, job_id: str):
        """Return a job to queued only when no provider request was made."""
        if self.next_available_window(job_id) is None:
            raise ValueError('Tarefa sem janela adiada registrada')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM attempts WHERE job_id=? LIMIT 1', (job_id,)).fetchone():
                db.rollback()
                raise ValueError('Tarefa com chamada iniciada não pode voltar à fila')
            changed=db.execute('UPDATE jobs SET status=?,updated_at=? WHERE job_id=? AND status=?',
                ('queued',_time(),job_id,'running')).rowcount
            if changed!=1:
                db.rollback()
                raise ValueError('Só uma tarefa sem chamadas pode voltar à fila')
            db.commit()

    def complete_job(self, job_id: str, result_path: Path, result_sha256: str):
        result_path = Path(result_path).resolve()
        if not result_path.is_file() or sha256(result_path.read_bytes()).hexdigest() != result_sha256:
            raise ValueError('Resultado da tarefa ausente ou hash divergente')
        with self._connect() as db:
            changed = db.execute('UPDATE jobs SET status=?,result_path=?,result_sha256=?,updated_at=? WHERE job_id=? AND status=?',
                ('complete', str(result_path), result_sha256, _time(), job_id, 'running')).rowcount
            if changed != 1:
                raise ValueError('Tarefa não está em execução no ledger')

    def incomplete_job(self, job_id: str, result_path: Path, result_sha256: str):
        result_path = Path(result_path).resolve()
        if not result_path.is_file() or sha256(result_path.read_bytes()).hexdigest() != result_sha256:
            raise ValueError('Registro incompleto da tarefa ausente ou hash divergente')
        with self._connect() as db:
            changed = db.execute('UPDATE jobs SET status=?,result_path=?,result_sha256=?,updated_at=? WHERE job_id=? AND status=?',
                ('incomplete', str(result_path), result_sha256, _time(), job_id, 'running')).rowcount
            if changed != 1:
                raise ValueError('Tarefa não está em execução no ledger')

    def reserve_attempt(self, *, model: str, job_id: str, payload_sha256: str,
                        tokens: int, now: datetime | None = None) -> str:
        if type(tokens) is not int or tokens <= 0:
            raise ValueError('Reserva de tokens inválida')
        if (self.window.tokens_per_minute is not None and tokens > self.window.tokens_per_minute) or (
            self.enforce_tokens_per_day and self.window.tokens_per_day is not None and tokens > self.window.tokens_per_day):
            raise ExpectedServiceError('free_quota_request_too_large',
                'pedido individual excede a menor cota gratuita por janela e não pode ser adiado')
        instant = now or datetime.now(timezone.utc)
        now_text = _time(instant)
        minute_cutoff = _time(instant - timedelta(seconds=60))
        day_cutoff = _time(instant - timedelta(seconds=86400))
        attempt_id = str(uuid4())
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT status FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if job is not None and job['status'] != 'running':
                raise ValueError('Tentativa exige tarefa em execução; encerramento não pode receber novas chamadas')
            rows = db.execute('''SELECT created_at,reserved_tokens,actual_tokens,status FROM attempts
                WHERE created_at >= ? ORDER BY created_at''', (day_cutoff,)).fetchall()
            day_rows = rows
            minute_rows = [row for row in rows if row['created_at'] >= minute_cutoff]
            used_day_tokens = sum(row['actual_tokens'] if row['status'] == 'reported'
                                  else row['reserved_tokens'] for row in day_rows)
            used_minute_tokens = sum(row['actual_tokens'] if row['status'] == 'reported'
                                     else row['reserved_tokens'] for row in minute_rows)
            blocked=[]
            if self.window.requests_per_day is not None and len(day_rows) >= self.window.requests_per_day:
                index=len(day_rows)-self.window.requests_per_day
                blocked.append(('RPD',datetime.fromisoformat(day_rows[index]['created_at'])+timedelta(seconds=86400)))
            if self.enforce_tokens_per_day and self.window.tokens_per_day is not None and used_day_tokens + tokens > self.window.tokens_per_day:
                blocked.append(('TPD',self._token_release(day_rows,tokens,self.window.tokens_per_day,86400)))
            if self.window.requests_per_minute is not None and len(minute_rows) >= self.window.requests_per_minute:
                index=len(minute_rows)-self.window.requests_per_minute
                blocked.append(('RPM',datetime.fromisoformat(minute_rows[index]['created_at'])+timedelta(seconds=60)))
            if self.window.tokens_per_minute is not None and used_minute_tokens + tokens > self.window.tokens_per_minute:
                blocked.append(('TPM',self._token_release(minute_rows,tokens,self.window.tokens_per_minute,60)))
            if blocked:
                db.rollback()
                reason=' + '.join(name for name,_ in blocked)
                available=max(at for _,at in blocked)
                with self._connect() as deferred_db:
                    deferred_db.execute('''INSERT INTO deferred_windows VALUES (?,?,?,?)
                        ON CONFLICT(job_id) DO UPDATE SET available_at=excluded.available_at,
                        reason=excluded.reason,updated_at=excluded.updated_at''',
                        (job_id,_time(available),reason,now_text))
                raise ExpectedServiceError('free_quota_deferred',
                    f'{reason} gratuito reservado/esgotado; próxima janela local disponível em {_time(available)}')
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?)',
                (attempt_id, now_text, model, job_id, payload_sha256, tokens,
                 None, 'reserved', None))
            db.commit()
        return attempt_id

    @staticmethod
    def _token_release(rows, incoming: int, limit: int, window_seconds: int):
        used=[row['actual_tokens'] if row['status']=='reported' else row['reserved_tokens'] for row in rows]
        remaining=sum(used)+incoming
        for row,value in zip(rows,used):
            remaining-=value
            if remaining<=limit:
                return datetime.fromisoformat(row['created_at'])+timedelta(seconds=window_seconds)
        return datetime.now(timezone.utc)+timedelta(seconds=window_seconds)

    def settle_attempt(self, attempt_id: str, *, actual_tokens: int | None,
                       status: str, http_status: int | None = None):
        if status not in {'reported', 'unknown'}:
            raise ValueError('Estado de consumo do ledger inválido')
        if status == 'reported' and (type(actual_tokens) is not int or actual_tokens < 0):
            raise ValueError('Consumo reportado inválido')
        with self._connect() as db:
            changed = db.execute('UPDATE attempts SET status=?,actual_tokens=?,http_status=? WHERE id=? AND status=?',
                (status, actual_tokens if status == 'reported' else None,
                 http_status, attempt_id, 'reserved')).rowcount
            if changed != 1:
                raise ValueError('Tentativa ausente ou já liquidada')

    def snapshot(self, *, now: datetime | None = None):
        instant = now or datetime.now(timezone.utc)
        cutoff = _time(instant - timedelta(seconds=86400))
        with self._connect() as db:
            rows = db.execute('SELECT * FROM attempts WHERE created_at>=? ORDER BY created_at',
                              (cutoff,)).fetchall()
        return {'window': self.window.__dict__, 'attempts_24h': len(rows),
            'reported_tokens': sum(row['actual_tokens'] or 0 for row in rows if row['status']=='reported'),
            'unknown_reserved_tokens': sum(row['reserved_tokens'] for row in rows if row['status']!='reported'),
            'jobs': self._job_counts()}

    def _job_counts(self):
        with self._connect() as db:
            return {row['status']: row['count'] for row in db.execute(
                'SELECT status,COUNT(*) AS count FROM jobs GROUP BY status')}
