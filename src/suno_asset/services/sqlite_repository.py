"""Transactional local runtime index. No provider call is replayed by recovery."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from suno_asset.services.protocols import ExpectedServiceError

_SCHEMA = 2
_ACTIVE = ('registered', 'running')
_METRICS = frozenset({'duration_ms', 'pass_number', 'pages', 'units', 'chunks', 'evidence', 'anchors', 'attempt_id', 'provider', 'model', 'input_tokens', 'output_tokens', 'total_tokens', 'estimated_tokens', 'unknown_tokens', 'nominal_cost', 'authorized_cost', 'error_code', 'iteration', 'token_status', 'ledger_id', 'prompt_tokens', 'completion_tokens', 'reserved_tokens', 'usage_known', 'attempt', 'nominal_cost_usd', 'authorized_billing_usd'})
_LABEL = re.compile(r'^[A-Za-z0-9_./:-]{1,160}$')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid(value: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ExpectedServiceError('invalid_resource_id', 'Invalid resource identifier.') from None


def _label(value: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ExpectedServiceError('invalid_event', 'Invalid operational event.')
    return value


def _identity(pid: int | None = None) -> dict[str, Any]:
    pid = pid or os.getpid()
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        # comm can contain spaces and parentheses; fields after its final ')' start at #3.
        start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        boot, start = '', ''
    return {'pid': pid, 'boot_id': boot, 'start_ticks': start}


def _alive(owner: dict[str, Any]) -> bool:
    current = _identity(owner['pid'])
    if owner.get('boot_id') and owner.get('start_ticks'):
        return current == owner
    # Unknown identity is conservative: do not declare another process dead.
    try:
        os.kill(owner['pid'], 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class SQLiteRepository:
    """Each operation owns a connection; safe across threads and forked processes."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'runtime.sqlite3'
        if self.path.is_symlink():
            raise ExpectedServiceError('unsafe_storage_path', 'Unsafe runtime database.')
        with self._connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > _SCHEMA:
                raise ExpectedServiceError('unsupported_schema_version', 'Runtime database requires newer application.')
            if version == 0:
                conn.executescript('''BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,state TEXT NOT NULL DEFAULT 'available',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),state TEXT NOT NULL DEFAULT 'available');
CREATE TABLE IF NOT EXISTS executions(request_id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),request_hash TEXT NOT NULL,mode TEXT NOT NULL,state TEXT NOT NULL,configuration TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT NOT NULL,deadline_at REAL NOT NULL,finished_at TEXT,owner TEXT,result_path TEXT,error_code TEXT,http_status INTEGER);
CREATE TABLE IF NOT EXISTS idempotency(key_digest TEXT PRIMARY KEY,request_hash TEXT NOT NULL,request_id TEXT NOT NULL REFERENCES executions(request_id));
CREATE TABLE IF NOT EXISTS artifacts(path TEXT PRIMARY KEY,digest TEXT NOT NULL,size INTEGER NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_owners(path TEXT NOT NULL REFERENCES artifacts(path),kind TEXT NOT NULL,owner_id TEXT NOT NULL,PRIMARY KEY(path,kind,owner_id));
CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL REFERENCES executions(request_id),node TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,metrics TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_attempts(attempt_id TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES executions(request_id),stage TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,metrics TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deletion_jobs(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),state TEXT NOT NULL,created_at TEXT NOT NULL,finished_at TEXT,error_code TEXT);
CREATE INDEX IF NOT EXISTS executions_source ON executions(source_id);
CREATE INDEX IF NOT EXISTS artifact_owners_target ON artifact_owners(kind,owner_id);
PRAGMA user_version=1;
COMMIT;''')
        with self._transaction() as conn:
            # Re-read under the write lock: another process may have migrated meanwhile.
            if conn.execute('PRAGMA user_version').fetchone()[0] == 1:
                conn.execute('ALTER TABLE artifacts ADD COLUMN writer TEXT')
                conn.execute('PRAGMA user_version=2')

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        try:
            yield conn
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                raise ExpectedServiceError('storage_busy', 'Local runtime storage is busy.') from None
            raise ExpectedServiceError('storage_unavailable', 'Local runtime storage is unavailable.') from None
        finally:
            conn.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _run(row):
        if row is None:
            return None
        result = dict(row)
        result['configuration'] = json.loads(result['configuration'])
        result['owner'] = json.loads(result['owner']) if result['owner'] else None
        return result

    def register_source(self, source_id: str) -> None:
        source_id = _uuid(source_id)
        with self._transaction() as conn:
            conn.execute('INSERT OR IGNORE INTO sources(id,created_at) VALUES(?,?)', (source_id, _now()))
            self._available(conn, 'source', source_id)

    def register_document(self, document_id: str, source_id: str) -> None:
        document_id, source_id = _uuid(document_id), _uuid(source_id)
        with self._transaction() as conn:
            self._available(conn, 'source', source_id)
            conn.execute('INSERT OR IGNORE INTO documents(id,source_id) VALUES(?,?)', (document_id, source_id))
            row = conn.execute('SELECT source_id FROM documents WHERE id=?', (document_id,)).fetchone()
            if row['source_id'] != source_id:
                raise ExpectedServiceError('document_identity_conflict', 'Document identity is already registered.')
            self._available(conn, 'document', document_id)

    def _available(self, conn, kind: str, resource_id: str) -> None:
        table, key = {'source': ('sources', 'id'), 'document': ('documents', 'id'), 'run': ('executions', 'request_id')}[kind]
        row = conn.execute(f'SELECT state FROM {table} WHERE {key}=?', (resource_id,)).fetchone()
        if row is None:
            raise ExpectedServiceError('resource_not_found', 'Resource is not registered.')
        if row['state'] in ('deleting', 'deleted'):
            raise ExpectedServiceError('resource_deleted', 'Resource was deleted or is being deleted.')

    def ensure_available(self, kind: str, resource_id: str) -> None:
        with self._connection() as conn:
            self._available(conn, kind, _uuid(resource_id))

    def ensure_run(self, request_id: str, source_id: str, mode: str = 'simulated', configuration: dict | None = None) -> dict:
        request_id, source_id = _uuid(request_id), _uuid(source_id)
        configuration_json = json.dumps(configuration or {}, sort_keys=True)
        with self._transaction() as conn:
            self._available(conn, 'source', source_id)
            row = conn.execute('SELECT * FROM executions WHERE request_id=?', (request_id,)).fetchone()
            if row is not None:
                self._available(conn, 'run', request_id)
                if row['source_id'] != source_id or row['mode'] != mode or row['configuration'] != configuration_json:
                    raise ExpectedServiceError('execution_identity_conflict', 'Execution identity is already registered.')
                if row['state'] == 'completed':
                    return self._run(row)
                if row['state'] == 'interrupted':
                    raise ExpectedServiceError('execution_interrupted', 'Execution was interrupted; automatic replay is forbidden.')
                if row['state'] != 'registered' or json.loads(row['owner']) != _identity():
                    raise ExpectedServiceError('generation_unavailable', 'Execution is already running or unavailable.')
                conn.execute("UPDATE executions SET state='running' WHERE request_id=?", (request_id,))
            else:
                active = conn.execute("SELECT count(*) FROM executions WHERE mode=? AND state IN ('registered','running')", (mode,)).fetchone()[0]
                if active >= (1 if mode == 'real' else 2):
                    raise ExpectedServiceError('execution_capacity_exhausted', 'Local execution capacity is temporarily exhausted.')
                now = _now()
                conn.execute('INSERT INTO executions(request_id,source_id,request_hash,mode,state,configuration,created_at,started_at,deadline_at,owner) VALUES(?,?,?,?,?,?,?,?,?,?)', (request_id, source_id, '', mode, 'running', configuration_json, now, now, datetime.now(timezone.utc).timestamp() + 2400, json.dumps(_identity())))
            return self._run(conn.execute('SELECT * FROM executions WHERE request_id=?', (request_id,)).fetchone())

    def register_legacy_run(self, request_id: str, source_id: str, mode: str = 'simulated', configuration: dict | None = None) -> dict:
        """Index historical output without claiming capacity or inventing an active owner."""
        request_id, source_id = _uuid(request_id), _uuid(source_id)
        with self._transaction() as conn:
            self._available(conn, 'source', source_id)
            now = _now()
            config = {**(configuration or {}), 'legacy': True, 'audit_imported': False}
            conn.execute('INSERT OR IGNORE INTO executions(request_id,source_id,request_hash,mode,state,configuration,created_at,started_at,deadline_at,owner) VALUES(?,?,?,?,?,?,?,?,?,NULL)', (request_id, source_id, '', mode, 'interrupted', json.dumps(config, sort_keys=True), now, now, 0))
            self._available(conn, 'run', request_id)
            row = conn.execute('SELECT * FROM executions WHERE request_id=?', (request_id,)).fetchone()
            if row['source_id'] != source_id or row['mode'] != mode:
                raise ExpectedServiceError('execution_identity_conflict', 'Execution identity is already registered.')
            return self._run(row)

    def claim(self, source_id: str, request_hash: str, mode: str, *, key: str | None = None, configuration: dict | None = None) -> tuple[dict, bool]:
        source_id = _uuid(source_id)
        if mode not in ('real', 'simulated'):
            raise ExpectedServiceError('invalid_mode', 'Invalid execution mode.')
        self.reconcile_owners()
        digest = hashlib.sha256(key.encode()).hexdigest() if key is not None else None
        with self._transaction() as conn:
            self._available(conn, 'source', source_id)
            if digest:
                prior = conn.execute('SELECT * FROM idempotency WHERE key_digest=?', (digest,)).fetchone()
                if prior:
                    if prior['request_hash'] != request_hash:
                        raise ExpectedServiceError('idempotency_conflict', 'Idempotency key belongs to another request.')
                    row = conn.execute('SELECT * FROM executions WHERE request_id=?', (prior['request_id'],)).fetchone()
                    return self._run(row), False
            active = conn.execute("SELECT count(*) FROM executions WHERE mode=? AND state IN ('registered','running')", (mode,)).fetchone()[0]
            if active >= (1 if mode == 'real' else 2):
                raise ExpectedServiceError('execution_capacity_exhausted', 'Local execution capacity is temporarily exhausted.')
            request_id, now = str(uuid4()), _now()
            conn.execute('INSERT INTO executions(request_id,source_id,request_hash,mode,state,configuration,created_at,started_at,deadline_at,owner) VALUES(?,?,?,?,?,?,?,?,?,?)', (request_id, source_id, request_hash, mode, 'registered', json.dumps(configuration or {}, sort_keys=True), now, now, datetime.now(timezone.utc).timestamp() + 2400, json.dumps(_identity())))
            if digest:
                conn.execute('INSERT INTO idempotency VALUES(?,?,?)', (digest, request_hash, request_id))
            return self._run(conn.execute('SELECT * FROM executions WHERE request_id=?', (request_id,)).fetchone()), True

    def get_run(self, request_id: str) -> dict | None:
        with self._connection() as conn:
            return self._run(conn.execute('SELECT * FROM executions WHERE request_id=?', (_uuid(request_id),)).fetchone())

    def reconcile_owners(self) -> list[str]:
        changed = []
        with self._transaction() as conn:
            for row in conn.execute("SELECT request_id,owner FROM executions WHERE state IN ('registered','running')").fetchall():
                owner = json.loads(row['owner']) if row['owner'] else None
                if owner and not _alive(owner):
                    conn.execute("UPDATE executions SET state='interrupted',error_code='execution_interrupted',http_status=503,finished_at=? WHERE request_id=?", (_now(), row['request_id']))
                    conn.execute("UPDATE provider_attempts SET status='unknown',finished_at=? WHERE request_id=? AND status='started'", (_now(), row['request_id']))
                    changed.append(row['request_id'])
        return changed

    def finish_run(self, request_id: str, result_path: str | None, error_code: str | None = None, http_status: int | None = None) -> None:
        with self._transaction() as conn:
            self._available(conn, 'run', _uuid(request_id))
            conn.execute('UPDATE executions SET state=?,result_path=?,error_code=?,http_status=?,finished_at=? WHERE request_id=?', ('errored' if error_code else 'completed', result_path, _label(error_code) if error_code else None, http_status or (500 if error_code else 200), _now(), str(request_id)))

    def fail_run(self, request_id: str, error_code: str, http_status: int = 500) -> None:
        self.finish_run(request_id, None, error_code, http_status)

    def _relative(self, relative: str) -> str:
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or not path.parts or '\\' in relative:
            raise ExpectedServiceError('unsafe_storage_path', 'Unsafe artifact path.')
        target = self.root.joinpath(*path.parts)
        if any(part.is_symlink() for part in [target, *target.parents] if part != self.root.parent and self.root in (part, *part.parents)):
            raise ExpectedServiceError('unsafe_storage_path', 'Unsafe artifact path.')
        return path.as_posix()

    def begin_artifact(self, relative: str, digest: str, size: int, owners: list[tuple[str, str]], replace: bool = False) -> None:
        relative = self._relative(relative)
        if not re.fullmatch(r'[0-9a-f]{64}', digest) or not isinstance(size, int) or size < 0 or not owners:
            raise ExpectedServiceError('invalid_artifact', 'Invalid artifact metadata.')
        with self._transaction() as conn:
            for kind, owner_id in owners:
                try:
                    self._available(conn, kind, _uuid(owner_id))
                except ExpectedServiceError as exc:
                    if exc.code != 'resource_not_found':
                        raise
            prior = conn.execute('SELECT * FROM artifacts WHERE path=?', (relative,)).fetchone()
            if prior and prior['state'] == 'pending' and prior['writer']:
                writer = json.loads(prior['writer'])
                if writer != _identity() and _alive(writer):
                    raise ExpectedServiceError('storage_busy', 'Artifact has an active writer.')
            if prior and (prior['digest'] != digest or prior['size'] != size):
                if not replace or prior['state'] == 'removed':
                    raise ExpectedServiceError('artifact_conflict', 'Artifact identity cannot be replaced.')
                conn.execute("UPDATE artifacts SET digest=?,size=?,state='pending' WHERE path=?", (digest, size, relative))
            elif not prior:
                conn.execute("INSERT INTO artifacts(path,digest,size,state,created_at) VALUES(?,?,?,'pending',?)", (relative, digest, size, _now()))
            elif prior['state'] == 'removed':
                raise ExpectedServiceError('resource_deleted', 'Artifact was deleted.')
            conn.execute("UPDATE artifacts SET writer=?,state='pending' WHERE path=?", (json.dumps(_identity()), relative))
            for kind, owner_id in owners:
                conn.execute('INSERT OR IGNORE INTO artifact_owners VALUES(?,?,?)', (relative, kind, _uuid(owner_id)))

    def _verify(self, row: dict) -> bool:
        try:
            path = self.root / self._relative(row['path'])
            raw = path.read_bytes()
            return len(raw) == row['size'] and hashlib.sha256(raw).hexdigest() == row['digest']
        except (OSError, ExpectedServiceError):
            return False

    def publish_artifact(self, relative: str) -> None:
        relative = self._relative(relative)
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM artifacts WHERE path=?', (relative,)).fetchone()
            if row and row['state'] == 'pending' and row['writer']:
                writer = json.loads(row['writer'])
                if writer != _identity() and _alive(writer):
                    raise ExpectedServiceError('storage_busy', 'Artifact has an active writer.')
            if not row or row['state'] == 'removed' or not self._verify(dict(row)):
                raise ExpectedServiceError('artifact_integrity_error', 'Artifact is missing or corrupt.')
            for owner in conn.execute('SELECT kind,owner_id FROM artifact_owners WHERE path=?', (relative,)):
                try:
                    self._available(conn, owner['kind'], owner['owner_id'])
                except ExpectedServiceError as exc:
                    if exc.code != 'resource_not_found':
                        raise
            conn.execute("UPDATE artifacts SET state='available',writer=NULL WHERE path=?", (relative,))

    def get_artifact(self, relative: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM artifacts WHERE path=?', (self._relative(relative),)).fetchone()
            return dict(row) if row else None

    def ensure_artifact_available(self, relative: str) -> dict:
        relative = self._relative(relative)
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM artifacts WHERE path=?', (relative,)).fetchone()
            if row is None or row['state'] != 'available':
                raise ExpectedServiceError('artifact_unavailable', 'Artifact is not available in the runtime index.')
            owners = conn.execute('SELECT kind,owner_id FROM artifact_owners WHERE path=?', (relative,)).fetchall()
            accessible = False
            for owner in owners:
                try:
                    self._available(conn, owner['kind'], owner['owner_id'])
                    accessible = True
                except ExpectedServiceError as exc:
                    if exc.code == 'resource_not_found':
                        accessible = True
                    elif exc.code != 'resource_deleted':
                        raise
            if not accessible:
                raise ExpectedServiceError('resource_deleted', 'Artifact owners are unavailable.')
            return dict(row)

    def artifacts_for(self, kind: str, resource_id: str) -> list[dict]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute('SELECT a.* FROM artifacts a JOIN artifact_owners o ON a.path=o.path WHERE o.kind=? AND o.owner_id=? ORDER BY a.path', (kind, _uuid(resource_id)))]

    def list_artifacts(self) -> list[dict]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM artifacts ORDER BY path')]

    def commit_result(self, request_id: str, relative: str, mandatory_paths: list[str] | tuple[str, ...]) -> None:
        request_id, relative = _uuid(request_id), self._relative(relative)
        with self._transaction() as conn:
            self._available(conn, 'run', request_id)
            for path in set([relative, *mandatory_paths]):
                path = self._relative(path)
                row = conn.execute('SELECT a.* FROM artifacts a JOIN artifact_owners o ON a.path=o.path WHERE a.path=? AND o.kind=? AND o.owner_id=?', (path, 'run', request_id)).fetchone()
                if not row or row['state'] != 'available' or not self._verify(dict(row)):
                    raise ExpectedServiceError('artifact_integrity_error', 'Required execution artifact is missing or corrupt.')
            conn.execute("UPDATE executions SET state='completed',result_path=?,http_status=200,finished_at=?,error_code=NULL WHERE request_id=?", (relative, _now(), request_id))

    def event(self, request_id: str, node: str, status: str, **metrics: Any) -> None:
        self._validate_metrics(metrics)
        with self._transaction() as conn:
            self._available(conn, 'run', _uuid(request_id))
            conn.execute('INSERT INTO events(request_id,node,status,created_at,metrics) VALUES(?,?,?,?,?)', (str(request_id), _label(node), _label(status), _now(), json.dumps(metrics, sort_keys=True, allow_nan=False)))

    @staticmethod
    def _validate_metrics(metrics):
        if set(metrics) - _METRICS:
            raise ExpectedServiceError('invalid_event', 'Operational event contains unapproved fields.')
        for key, value in metrics.items():
            if value is None:
                continue
            if isinstance(value, str):
                valid = bool(_LABEL.fullmatch(value))
            elif isinstance(value, (int, float)):
                valid = math.isfinite(value) and value >= 0
            else:
                valid = False
            if not valid:
                raise ExpectedServiceError('invalid_event', 'Operational event contains invalid values.')

    def get_events(self, request_id: str) -> list[dict]:
        with self._connection() as conn:
            rows = [dict(row) for row in conn.execute('SELECT * FROM events WHERE request_id=? ORDER BY sequence', (_uuid(request_id),))]
            for row in rows:
                row['metrics'] = json.loads(row['metrics'])
            return rows

    def record_attempt(self, request_id: str, attempt_id: str, stage: str, model: str) -> None:
        with self._transaction() as conn:
            self._available(conn, 'run', _uuid(request_id))
            conn.execute("INSERT INTO provider_attempts VALUES(?,?,?,?,'started',?,NULL,'{}')", (_label(attempt_id), str(request_id), _label(stage), _label(model), _now()))

    def finish_attempt(self, attempt_id: str, status: str, **metrics: Any) -> None:
        self._validate_metrics(metrics)
        if status not in ('confirmed', 'unknown', 'failed'):
            raise ExpectedServiceError('invalid_attempt', 'Invalid provider attempt state.')
        with self._transaction() as conn:
            updated = conn.execute('UPDATE provider_attempts SET status=?,finished_at=?,metrics=? WHERE attempt_id=?', (status, _now(), json.dumps(metrics, allow_nan=False), _label(attempt_id)))
            if not updated.rowcount:
                raise ExpectedServiceError('invalid_attempt', 'Provider attempt is not registered.')

    def attempts_for(self, request_id: str) -> list[dict]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM provider_attempts WHERE request_id=? ORDER BY started_at', (_uuid(request_id),))]

    def deletion_preview(self, source_id: str) -> dict:
        source_id = _uuid(source_id)
        with self._connection() as conn:
            source = conn.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
            if not source:
                raise ExpectedServiceError('resource_not_found', 'Source is not registered.')
            documents = [r[0] for r in conn.execute('SELECT id FROM documents WHERE source_id=?', (source_id,))]
            runs = [r[0] for r in conn.execute('SELECT request_id FROM executions WHERE source_id=?', (source_id,))]
            owners = {('source', source_id), *(('document', doc) for doc in documents), *(('run', run) for run in runs)}
            rows = conn.execute('SELECT * FROM artifact_owners').fetchall()
            affected = {r['path'] for r in rows if (r['kind'], r['owner_id']) in owners}
            removable = sorted(path for path in affected if all((r['kind'], r['owner_id']) in owners for r in rows if r['path'] == path))
            return {'source_id': source_id, 'state': source['state'], 'documents': documents, 'runs': runs, 'artifacts': removable, 'shared_artifacts': sorted(affected - set(removable)), 'scope': 'managed_storage_only'}

    def run_deletion_preview(self, request_id: str) -> dict:
        request_id = _uuid(request_id)
        with self._connection() as conn:
            run = conn.execute('SELECT * FROM executions WHERE request_id=?', (request_id,)).fetchone()
            if run is None:
                raise ExpectedServiceError('resource_not_found', 'Execution is not registered.')
            rows = conn.execute('SELECT path,kind,owner_id FROM artifact_owners').fetchall()
            affected = {r['path'] for r in rows if r['kind'] == 'run' and r['owner_id'] == request_id}
            removable = sorted(path for path in affected if all(
                r['kind'] == 'run' and r['owner_id'] == request_id for r in rows if r['path'] == path))
            return {'request_id':request_id, 'source_id':run['source_id'], 'state':run['state'],
                'artifacts':removable, 'shared_artifacts':sorted(affected - set(removable)),
                'scope':'execution_only'}

    def begin_run_deletion(self, request_id: str) -> None:
        request_id = _uuid(request_id)
        with self._transaction() as conn:
            run = conn.execute('SELECT state FROM executions WHERE request_id=?', (request_id,)).fetchone()
            if run is None:
                raise ExpectedServiceError('resource_not_found', 'Execution is not registered.')
            if run['state'] in {'deleting', 'deleted'}:
                return
            if run['state'] in _ACTIVE:
                raise ExpectedServiceError('resource_busy', 'Execution is still active.')
            writers = conn.execute('''SELECT a.writer FROM artifacts a JOIN artifact_owners o ON a.path=o.path
WHERE o.kind='run' AND o.owner_id=? AND a.writer IS NOT NULL''', (request_id,)).fetchall()
            if any(_alive(json.loads(row['writer'])) for row in writers):
                raise ExpectedServiceError('resource_busy', 'Execution has an active artifact writer.')
            conn.execute("UPDATE executions SET state='deleting' WHERE request_id=?", (request_id,))

    def complete_run_deletion(self, request_id: str) -> None:
        request_id = _uuid(request_id)
        with self._transaction() as conn:
            row = conn.execute('SELECT state FROM executions WHERE request_id=?', (request_id,)).fetchone()
            if row is None:
                raise ExpectedServiceError('resource_not_found', 'Execution is not registered.')
            if row['state'] not in {'deleting', 'deleted'}:
                raise ExpectedServiceError('resource_busy', 'Execution deletion has not started.')
            conn.execute('DELETE FROM events WHERE request_id=?', (request_id,))
            conn.execute('DELETE FROM provider_attempts WHERE request_id=?', (request_id,))
            conn.execute("UPDATE executions SET state='deleted',configuration='{}',request_hash='',result_path=NULL,error_code='resource_deleted',http_status=410,owner=NULL WHERE request_id=?", (request_id,))

    def begin_deletion(self, source_id: str) -> dict:
        source_id = _uuid(source_id)
        with self._transaction() as conn:
            prior = conn.execute("SELECT * FROM deletion_jobs WHERE source_id=? AND state!='completed' ORDER BY created_at LIMIT 1", (source_id,)).fetchone()
            if prior:
                return dict(prior)
            self._available(conn, 'source', source_id)
            if conn.execute("SELECT 1 FROM executions WHERE source_id=? AND state IN ('registered','running')", (source_id,)).fetchone():
                raise ExpectedServiceError('resource_busy', 'Source has an active execution.')
            writer_rows = conn.execute('''SELECT DISTINCT a.writer FROM artifacts a JOIN artifact_owners o ON a.path=o.path
WHERE a.writer IS NOT NULL AND (o.kind='source' AND o.owner_id=? OR
 o.kind='document' AND o.owner_id IN (SELECT id FROM documents WHERE source_id=?) OR
 o.kind='run' AND o.owner_id IN (SELECT request_id FROM executions WHERE source_id=?))''', (source_id, source_id, source_id)).fetchall()
            if any(_alive(json.loads(row['writer'])) for row in writer_rows):
                raise ExpectedServiceError('resource_busy', 'Source has an active artifact writer.')
            job_id = str(uuid4())
            conn.execute("INSERT INTO deletion_jobs(id,source_id,state,created_at) VALUES(?,?,'pending',?)", (job_id, source_id, _now()))
            conn.execute("UPDATE sources SET state='deleting' WHERE id=?", (source_id,))
            conn.execute("UPDATE documents SET state='deleting' WHERE source_id=?", (source_id,))
            conn.execute("UPDATE executions SET state='deleting' WHERE source_id=?", (source_id,))
            return dict(conn.execute('SELECT * FROM deletion_jobs WHERE id=?', (job_id,)).fetchone())

    def mark_artifact_removed(self, relative: str) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE artifacts SET state='removed',writer=NULL WHERE path=?", (self._relative(relative),))

    def complete_deletion(self, job_id: str) -> None:
        with self._transaction() as conn:
            job = conn.execute('SELECT * FROM deletion_jobs WHERE id=?', (_uuid(job_id),)).fetchone()
            if not job:
                raise ExpectedServiceError('resource_not_found', 'Deletion job is not registered.')
            source_id = job['source_id']
            # Keep identifiers/key digests only. Remove configuration, errors and all content-bearing event metadata.
            conn.execute('DELETE FROM events WHERE request_id IN (SELECT request_id FROM executions WHERE source_id=?)', (source_id,))
            conn.execute('DELETE FROM provider_attempts WHERE request_id IN (SELECT request_id FROM executions WHERE source_id=?)', (source_id,))
            conn.execute("UPDATE executions SET state='deleted',configuration='{}',request_hash='',result_path=NULL,error_code='resource_deleted',http_status=410,owner=NULL WHERE source_id=?", (source_id,))
            conn.execute("UPDATE documents SET state='deleted' WHERE source_id=?", (source_id,))
            conn.execute("UPDATE sources SET state='deleted' WHERE id=?", (source_id,))
            conn.execute("UPDATE deletion_jobs SET state='completed',finished_at=?,error_code=NULL WHERE id=?", (_now(), job_id))

    def fail_deletion(self, job_id: str, error_code: str) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE deletion_jobs SET state='pending',error_code=? WHERE id=?", (_label(error_code), _uuid(job_id)))

    def pending_deletions(self) -> list[dict]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM deletion_jobs WHERE state!='completed'")]

    def list_runs(self, state: str | None = None) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute('SELECT * FROM executions' + (' WHERE state=?' if state else '') + ' ORDER BY created_at', (state,) if state else ())
            return [self._run(row) for row in rows]

    def list_sources(self) -> list[dict]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM sources ORDER BY created_at')]

    def get_document(self, document_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM documents WHERE id=?', (_uuid(document_id),)).fetchone()
            return dict(row) if row else None

    def artifact_writer_alive(self, relative: str) -> bool | None:
        with self._connection() as conn:
            row = conn.execute('SELECT writer FROM artifacts WHERE path=?', (self._relative(relative),)).fetchone()
            if row is None or not row['writer']:
                return None
            return _alive(json.loads(row['writer']))

    def claim_abandoned_artifact(self, relative: str) -> bool:
        """Take an abandoned intent under the same lock used by new writers."""
        with self._transaction() as conn:
            row = conn.execute('SELECT state,writer FROM artifacts WHERE path=?', (self._relative(relative),)).fetchone()
            if row is None or row['state'] != 'pending' or not row['writer']:
                return False
            if _alive(json.loads(row['writer'])):
                return False
            conn.execute('UPDATE artifacts SET writer=? WHERE path=?', (json.dumps(_identity()), relative))
            return True
