"""Independent M10 integration review: persisted authority and no replay boundaries."""
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from suno_asset.domain.models import GenerationRequest
from suno_asset.services.artifact_lifecycle import delete_source, reconcile
from suno_asset.services.fakes import fake_services
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.parsing import ManualParser
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.sqlite_repository import SQLiteRepository
from suno_asset.workflow.graph import run_workflow

TEXT = 'O Copom decidiu reduzir a Selic para 13,75% ao ano.'


def test_runtime_index_is_authority_for_reads(tmp_path):
    store = LocalStorage(tmp_path)
    relative = 'raw/' + uuid4().hex + '.txt'
    (tmp_path / relative).write_text('unindexed source content')
    with pytest.raises(ExpectedServiceError) as exc:
        store._read(relative)
    assert exc.value.code == 'artifact_unavailable'


def test_begin_execution_consumes_registered_claim_only_once(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    row, _ = repo.claim(source, 'fingerprint', 'simulated', key='one-key', configuration={'mode': 'simulated'})
    assert row['state'] == 'registered'
    started = repo.ensure_run(row['request_id'], source, configuration={'mode': 'simulated'})
    assert started['state'] == 'running'
    with pytest.raises(ExpectedServiceError) as exc:
        repo.ensure_run(row['request_id'], source, configuration={'mode': 'simulated'})
    assert exc.value.code == 'generation_unavailable'


def test_completed_run_reuse_cannot_change_configuration(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source, request = str(uuid4()), str(uuid4())
    repo.register_source(source)
    repo.ensure_run(request, source, configuration={'architecture': 'literal'})
    content, relative = b'{}', 'runs/result.json'
    repo.begin_artifact(relative, hashlib.sha256(content).hexdigest(), 2, [('run', request)])
    (tmp_path / 'runs').mkdir()
    (tmp_path / relative).write_bytes(content)
    repo.publish_artifact(relative)
    repo.commit_result(request, relative, [])
    assert repo.ensure_run(request, source, configuration={'architecture': 'literal'})['state'] == 'completed'
    with pytest.raises(ExpectedServiceError) as exc:
        repo.ensure_run(request, source, configuration={'architecture': 'current'})
    assert exc.value.code == 'execution_identity_conflict'


def test_direct_workflow_repeat_has_no_new_nodes_or_inference(tmp_path):
    store = LocalStorage(tmp_path)
    source = store.put_text(TEXT)
    request = GenerationRequest(request_id=uuid4(), source=source)
    services = replace(fake_services(store), parser=ManualParser())
    first = run_workflow(request, services=services)
    count = len(store.repository.get_events(str(request.request_id)))
    again = run_workflow(request, services=services)
    assert again == first
    assert len(store.repository.get_events(str(request.request_id))) == count


def test_shared_file_preserved_but_deleted_source_cannot_read_it(tmp_path):
    store = LocalStorage(tmp_path)
    source, other = store.put_text(TEXT), store.put_text('Other live source.')
    for artifact in store.repository.artifacts_for('source', str(source.source_id)):
        store.repository.begin_artifact(artifact['path'], artifact['digest'], artifact['size'], [('source', str(other.source_id))])
        store.repository.publish_artifact(artifact['path'])
    delete_source(store, source.source_id, execute=True)
    for artifact in store.repository.artifacts_for('source', str(other.source_id)):
        assert (tmp_path / artifact['path']).exists()
        assert store.repository.ensure_artifact_available(artifact['path'])['state'] == 'available'
    with pytest.raises(ExpectedServiceError) as exc:
        store.read_raw(source)
    assert exc.value.code == 'resource_deleted'
    assert store.read_raw(other) == b'Other live source.'


def test_recovery_cannot_complete_result_with_missing_evidence(tmp_path):
    store = LocalStorage(tmp_path)
    source = store.put_text(TEXT)
    request = GenerationRequest(request_id=uuid4(), source=source)
    result = run_workflow(request, services=replace(fake_services(store), parser=ManualParser()))
    assert result.references
    next((tmp_path / 'evidence').glob('*.json')).unlink()
    with sqlite3.connect(store.repository.path) as conn:
        conn.execute("UPDATE executions SET state='interrupted' WHERE request_id=?", (str(request.request_id),))
    reconcile(store)
    assert store.repository.get_run(str(request.request_id))['state'] != 'completed'


def test_direct_workflow_admission_shares_api_reserved_capacity(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    claimed, _ = repo.claim(source, 'real-api', 'real', key='real-key')
    with pytest.raises(ExpectedServiceError) as exc:
        repo.ensure_run(str(uuid4()), source, mode='real')
    assert exc.value.code == 'execution_capacity_exhausted'
    assert repo.ensure_run(claimed['request_id'], source, mode='real')['state'] == 'running'
    repo.fail_run(claimed['request_id'], 'cancelled')
    assert repo.ensure_run(str(uuid4()), source, mode='real')['state'] == 'running'


def test_legacy_index_does_not_claim_execution_capacity_or_invent_owner(tmp_path):
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    for _ in range(4):
        row = repo.register_legacy_run(str(uuid4()), source, mode='real')
        assert row['state'] == 'interrupted'
        assert row['owner'] is None
        assert row['configuration']['legacy'] is True
        assert row['configuration']['audit_imported'] is False
    assert repo.claim(source, 'new-execution', 'real', key='fresh-key')[1] is True


def _crash_artifact_writer(root, source):
    repo = SQLiteRepository(root)
    repo.begin_artifact('raw/unfinished.txt', '0' * 64, 0, [('source', source)])


def test_artifact_pending_writer_has_process_identity_and_proven_death(tmp_path):
    import multiprocessing
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    repo.begin_artifact('raw/live.txt', '0' * 64, 0, [('source', source)])
    assert repo.artifact_writer_alive('raw/live.txt') is True
    ctx = multiprocessing.get_context('spawn')
    worker = ctx.Process(target=_crash_artifact_writer, args=(str(tmp_path), source))
    worker.start()
    worker.join(20)
    assert worker.exitcode == 0
    assert repo.artifact_writer_alive('raw/unfinished.txt') is False
    assert repo.artifact_writer_alive('raw/unknown.txt') is None


def _hold_artifact_writer(root, source, ready, release):
    repo = SQLiteRepository(root)
    repo.begin_artifact('raw/active.txt', '0' * 64, 0, [('source', source)])
    ready.set()
    release.wait(20)


def test_active_other_process_writer_cannot_be_stolen_or_cleaned(tmp_path):
    import multiprocessing
    repo = SQLiteRepository(tmp_path)
    source = str(uuid4())
    repo.register_source(source)
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold_artifact_writer, args=(str(tmp_path), source, ready, release))
    child.start()
    try:
        assert ready.wait(20)
        assert repo.claim_abandoned_artifact('raw/active.txt') is False
        with pytest.raises(ExpectedServiceError) as exc:
            repo.begin_artifact('raw/active.txt', '0' * 64, 0, [('source', source)])
        assert exc.value.code == 'storage_busy'
        with pytest.raises(ExpectedServiceError) as exc:
            repo.publish_artifact('raw/active.txt')
        assert exc.value.code == 'storage_busy'
        with pytest.raises(ExpectedServiceError) as exc:
            repo.begin_deletion(source)
        assert exc.value.code == 'resource_busy'
    finally:
        release.set()
        child.join(20)
        if child.is_alive():
            child.terminate()
            child.join()
    assert child.exitcode == 0
    assert repo.claim_abandoned_artifact('raw/active.txt') is True
    assert repo.artifact_writer_alive('raw/active.txt') is True
    assert repo.claim_abandoned_artifact('raw/active.txt') is False
