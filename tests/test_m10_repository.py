"""Durable ownership, admission, crash detection and artifact publication invariants."""
import hashlib
import multiprocessing
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.sqlite_repository import SQLiteRepository


def _contend(root, source, gate, output):
    repo = SQLiteRepository(root)
    gate.wait()
    row, owner = repo.claim(source, 'same-request', 'simulated', key='shared-key')
    output.put((row['request_id'], owner))
    # Keep owner alive while all contenders claim; parent releases it after collecting.
    gate.wait()


def _crash_owner(root, source, output):
    repo = SQLiteRepository(root)
    row, _ = repo.claim(source, 'crash-request', 'real', key='crashed-key')
    repo.record_attempt(row['request_id'], 'attempt-before-send', 'generate', 'model')
    output.put(row['request_id'])


def test_migration_reopen_and_future_version(tmp_path):
    repo = SQLiteRepository(tmp_path)
    assert SQLiteRepository(tmp_path).list_artifacts() == []
    with sqlite3.connect(repo.path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
        conn.execute('PRAGMA user_version=3')
    with pytest.raises(ExpectedServiceError, match='newer application') as exc:
        SQLiteRepository(tmp_path)
    assert exc.value.code == 'unsupported_schema_version'


def test_persistent_claim_conflict_capacity_and_no_expiration(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    run, owner = repo.claim(source, 'one', 'real', key='first-key')
    assert owner
    again, owner = SQLiteRepository(tmp_path).claim(source, 'one', 'real', key='first-key')
    assert not owner and again['request_id'] == run['request_id']
    assert again['deadline_at'] == run['deadline_at']
    with pytest.raises(ExpectedServiceError) as exc:
        repo.claim(source, 'changed', 'real', key='first-key')
    assert exc.value.code == 'idempotency_conflict'
    with pytest.raises(ExpectedServiceError) as exc:
        repo.claim(source, 'two', 'real', key='second-key')
    assert exc.value.code == 'execution_capacity_exhausted'
    repo.fail_run(run['request_id'], 'provider_unavailable', 503)
    again, owner = SQLiteRepository(tmp_path).claim(source, 'one', 'real', key='first-key')
    assert not owner and again['error_code'] == 'provider_unavailable'


def test_multiprocess_single_claim_owner(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    ctx = multiprocessing.get_context('spawn')
    gate, output = ctx.Barrier(5), ctx.Queue()
    workers = [ctx.Process(target=_contend, args=(str(tmp_path), source, gate, output)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        gate.wait(timeout=20)
        results = [output.get(timeout=20) for _ in workers]
        assert len({row[0] for row in results}) == 1
        assert sum(row[1] for row in results) == 1
        gate.wait(timeout=20)
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()


def test_dead_owner_marks_uncertain_attempt_without_replay(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    ctx = multiprocessing.get_context('spawn')
    output = ctx.Queue()
    child = ctx.Process(target=_crash_owner, args=(str(tmp_path), source, output))
    child.start()
    request_id = output.get(timeout=20)
    child.join(20)
    assert child.exitcode == 0
    assert repo.reconcile_owners() == [request_id]
    run, owner = repo.claim(source, 'crash-request', 'real', key='crashed-key')
    assert not owner and run['state'] == 'interrupted'
    assert repo.attempts_for(request_id)[0]['status'] == 'unknown'
    assert run['deadline_at'] == repo.get_run(request_id)['deadline_at']


def test_commit_requires_actual_artifacts_and_immutable_hash(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source, request_id = str(uuid4()), str(uuid4())
    repo.register_source(source)
    repo.ensure_run(request_id, source)
    content = b'{"valid": true}'
    digest = hashlib.sha256(content).hexdigest()
    path = 'runs/result.json'
    repo.begin_artifact(path, digest, len(content), [('run', request_id)])
    with pytest.raises(ExpectedServiceError):
        repo.publish_artifact(path)
    (tmp_path / 'runs').mkdir()
    (tmp_path / path).write_bytes(content)
    repo.publish_artifact(path)
    with pytest.raises(ExpectedServiceError):
        repo.commit_result(request_id, path, ['runs/missing-trace.json'])
    assert repo.get_run(request_id)['state'] == 'running'
    repo.commit_result(request_id, path, [])
    assert repo.get_run(request_id)['state'] == 'completed'
    (tmp_path / path).write_bytes(b'corruption')
    with pytest.raises(ExpectedServiceError):
        repo.commit_result(request_id, path, [])
    with pytest.raises(ExpectedServiceError):
        repo.begin_artifact(path, '0' * 64, 0, [('run', request_id)])


def test_deletion_tombstones_block_revival_and_preserve_other_owner(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source, other, doc = str(uuid4()), str(uuid4()), str(uuid4())
    repo.register_source(source)
    repo.register_source(other)
    repo.register_document(doc, source)
    run, _ = repo.claim(source, 'request', 'simulated', key='key')
    with pytest.raises(ExpectedServiceError) as exc:
        repo.begin_deletion(source)
    assert exc.value.code == 'resource_busy'
    repo.fail_run(run['request_id'], 'failed')
    repo.begin_artifact('raw/shared', hashlib.sha256(b'').hexdigest(), 0, [('source', source), ('source', other)])
    (tmp_path / 'raw').mkdir()
    (tmp_path / 'raw/shared').write_bytes(b'')
    repo.publish_artifact('raw/shared')
    preview = repo.deletion_preview(source)
    assert preview['shared_artifacts'] == ['raw/shared']
    job = repo.begin_deletion(source)
    assert repo.begin_deletion(source)['id'] == job['id']
    with pytest.raises(ExpectedServiceError) as exc:
        repo.claim(source, 'request', 'simulated', key='key')
    assert exc.value.code == 'resource_deleted'
    repo.complete_deletion(job['id'])
    assert repo.get_run(run['request_id'])['state'] == 'deleted'
    with pytest.raises(ExpectedServiceError):
        repo.register_source(source)
    with pytest.raises(ExpectedServiceError):
        repo.ensure_run(run['request_id'], source)
    repo.ensure_available('source', other)


def test_operational_events_reject_content_fields_and_unsafe_paths(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    run, _ = repo.claim(source, 'hash', 'simulated')
    repo.event(run['request_id'], 'parse', 'completed', duration_ms=12, pages=3)
    assert repo.get_events(run['request_id'])[0]['metrics']['pages'] == 3
    with pytest.raises(ExpectedServiceError):
        repo.event(run['request_id'], 'parse', 'error', prompt='secret')
    for path in ('../outside', '/absolute', 'raw/../../outside'):
        with pytest.raises(ExpectedServiceError):
            repo.begin_artifact(path, '0' * 64, 0, [('source', source)])
    (tmp_path / 'raw').symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ExpectedServiceError):
        repo.begin_artifact('raw/outside', '0' * 64, 0, [('source', source)])


def _distinct_claim(root, source, mode, gate, output):
    import time
    repo = SQLiteRepository(root)
    gate.wait()
    started = time.monotonic()
    try:
        row, owner = repo.claim(source, source, mode, key=source)
        output.put({'request_id': row['request_id'], 'source_id': row['source_id'], 'owner': owner, 'seconds': time.monotonic() - started})
    except ExpectedServiceError as exc:
        output.put({'error_code': exc.code, 'seconds': time.monotonic() - started})
    gate.wait()


def test_two_processes_distinct_sources_admission_and_isolation(tmp_path):
    import json
    repo = SQLiteRepository(tmp_path)
    ctx = multiprocessing.get_context('spawn')
    report = {}
    for mode in ('simulated', 'real'):
        root = tmp_path / mode
        repo = SQLiteRepository(root)
        sources = [str(uuid4()), str(uuid4())]
        for source in sources:
            repo.register_source(source)
        gate, output = ctx.Barrier(3), ctx.Queue()
        workers = [ctx.Process(target=_distinct_claim, args=(str(root), source, mode, gate, output)) for source in sources]
        try:
            for worker in workers:
                worker.start()
            gate.wait(timeout=20)
            rows = [output.get(timeout=20) for _ in workers]
            admitted = [row for row in rows if row.get('owner')]
            assert len(admitted) == (2 if mode == 'simulated' else 1)
            for row in admitted:
                assert repo.get_run(row['request_id'])['source_id'] == row['source_id']
            if mode == 'real':
                assert [r['error_code'] for r in rows if 'error_code' in r] == ['execution_capacity_exhausted']
            report[mode] = {'clients': 2, 'admitted': len(admitted), 'max_claim_seconds': max(row['seconds'] for row in rows), 'errors': [r['error_code'] for r in rows if 'error_code' in r], 'source_isolation': True}
            gate.wait(timeout=20)
            for worker in workers:
                worker.join(20)
                assert worker.exitcode == 0
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
    output_path = Path('data/m10-test-repository-concurrency.json')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + '\n')


def test_completed_result_claim_survives_reopen_and_owner_death(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    row, _ = repo.claim(source, 'effective-request', 'simulated', key='completed-key')
    content, path = b'{}', 'runs/result.json'
    repo.begin_artifact(path, hashlib.sha256(content).hexdigest(), len(content), [('run', row['request_id'])])
    (tmp_path / 'runs').mkdir()
    (tmp_path / path).write_bytes(content)
    repo.publish_artifact(path)
    repo.commit_result(row['request_id'], path, [])
    again, owner = SQLiteRepository(tmp_path).claim(source, 'effective-request', 'simulated', key='completed-key')
    assert owner is False
    assert again['state'] == 'completed'
    assert again['result_path'] == path
    assert again['request_id'] == row['request_id']
    assert again['http_status'] == 200


def test_active_artifact_writer_prevents_source_deletion(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    content, path = b'secret', 'raw/content.txt'
    repo.begin_artifact(path, hashlib.sha256(content).hexdigest(), len(content), [('source', source)])
    with pytest.raises(ExpectedServiceError) as exc:
        repo.begin_deletion(source)
    assert exc.value.code == 'resource_busy'
    (tmp_path / 'raw').mkdir()
    (tmp_path / path).write_bytes(content)
    repo.publish_artifact(path)
    repo.begin_deletion(source)
    with pytest.raises(ExpectedServiceError) as exc:
        repo.begin_artifact(path, hashlib.sha256(content).hexdigest(), len(content), [('source', source)])
    assert exc.value.code == 'resource_deleted'
