"""Operational contracts through the workflow/API, with no external inference."""
from dataclasses import replace
import json
import logging
import multiprocessing
import os
from pathlib import Path
from hashlib import sha256
import sqlite3
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from suno_asset.api.app import create_app
from suno_asset.domain.models import GenerationRequest
from suno_asset.services.artifact_lifecycle import delete_source, reconcile, inspect_orphans
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.fakes import fake_services
from suno_asset.services.parsing import ManualParser
from suno_asset.workflow.graph import run_workflow

TEXT = 'O Copom decidiu reduzir a Selic para 13,75% ao ano.'


def _produce_until_commit(root):
    store = LocalStorage(root)
    source = store.put_text(TEXT)
    def crash(*args, **kwargs):
        os._exit(7)
    store.repository.commit_result = crash
    run_workflow(GenerationRequest(request_id=uuid4(), source=source),
                 services=replace(fake_services(store), parser=ManualParser()))


def _abandoned_write(root):
    store = LocalStorage(root)
    relative = f'units/{UUID(int=999).hex}.json'
    store.repository.begin_artifact(relative, sha256(b'expected').hexdigest(), 8,
                                    [('document', str(UUID(int=999)))])
    (store.root / relative).write_bytes(b'partial')
    os._exit(9)


def _crash_before_rename(root):
    store = LocalStorage(root)
    source = store.put_text(TEXT)
    def crash(*args):
        os._exit(11)
    os.replace = crash
    store._write(source.artifact_ref, b'changed before crash', replace=True)


def generated(root):
    store = LocalStorage(root)
    client = TestClient(create_app(storage=store))
    source = client.post('/documents', json={'text': TEXT}).json()
    response = client.post('/generations', json={'source_id': source['source_id']},
                           headers={'Idempotency-Key': 'persistent-key'})
    assert response.status_code == 200, response.text
    return store, client, source, response.json()


def test_api_key_survives_restart_and_uses_persistent_status(tmp_path, monkeypatch):
    store, client, source, result = generated(tmp_path)
    def prohibit(_storage):
        raise AssertionError('must not construct services for a replay')
    restarted = TestClient(create_app(storage=LocalStorage(tmp_path), services_factory=prohibit))
    again = restarted.post('/generations', json={'source_id': source['source_id']},
                           headers={'Idempotency-Key': 'persistent-key'})
    assert again.status_code == 200 and again.json() == result
    status = restarted.get(f"/generations/{result['request_id']}/status").json()
    assert status['state'] == 'completed' and status['node'] == 'finalize'
    events = store.repository.get_events(result['request_id'])
    nodes = {event['node'] for event in events}
    assert {'ingest', 'parse', 'retrieve', 'generate', 'evaluate', 'finalize'} <= nodes
    assert all(any(e['status'] == 'started' for e in events if e['node'] == node) for node in nodes)
    metrics = [e['metrics'] for e in events]
    assert any(m.get('units', 0) > 0 for m in metrics)
    assert any(m.get('evidence', 0) > 0 for m in metrics)


def test_crash_after_result_before_commit_recovers_without_inference(tmp_path):
    process = multiprocessing.get_context('spawn').Process(target=_produce_until_commit, args=(str(tmp_path),))
    process.start()
    process.join(20)
    assert process.exitcode == 7
    store = LocalStorage(tmp_path)
    assert store.repository.list_runs()[0]['state'] == 'running'
    reconcile(store)
    row = store.repository.list_runs()[0]
    assert row['state'] == 'completed'
    assert store.load_execution_result(UUID(row['request_id'])).status.value == 'approved'
    assert store.repository.get_events(row['request_id'])[-1]['node'] == 'recovery'


@pytest.mark.parametrize('target', ['result', 'evidence'])
def test_missing_or_tampered_artifacts_never_serve_as_complete(tmp_path, target):
    store, client, source, result = generated(tmp_path)
    if target == 'result':
        path = store.root / f"runs/{UUID(result['request_id']).hex}-result.json"
        path.write_text('{}')
    else:
        next((store.root / 'evidence').glob('*.json')).unlink()
    response = client.get(f"/generations/{result['request_id']}")
    assert response.status_code == 503
    assert response.json()['detail']['error'] in {'artifact_unavailable', 'artifact_missing'}


def test_deletion_removes_all_derivatives_without_affecting_other_source(tmp_path):
    store, client, source, result = generated(tmp_path)
    other = store.put_text('Outro documento, que deve permanecer.')
    preview = delete_source(store, source['source_id'])
    assert len(preview['artifacts']) >= 8 and preview['runs'] == [result['request_id']]
    assert client.get(f"/generations/{result['request_id']}").status_code == 200
    delete_source(store, source['source_id'], execute=True)
    assert all(not (store.root / p).exists() for p in preview['artifacts'])
    assert store.load_source(other.source_id) == other
    assert client.get(f"/generations/{result['request_id']}").status_code == 410
    assert client.get(f"/generations/{result['request_id']}/evidence/0").status_code == 410
    replay = client.post('/generations', json={'source_id': source['source_id']},
                         headers={'Idempotency-Key': 'persistent-key'})
    assert replay.status_code == 410
    assert TestClient(create_app(storage=LocalStorage(tmp_path))).get(
        f"/generations/{result['request_id']}").status_code == 410


def test_partial_deletion_is_hidden_and_resumable(tmp_path, monkeypatch):
    store, client, source, result = generated(tmp_path)
    original = store._path
    def blocked(relative):
        if relative.startswith('runs/'):
            raise OSError('simulated failure')
        return original(relative)
    monkeypatch.setattr(store, '_path', blocked)
    with pytest.raises(ExpectedServiceError) as exc:
        delete_source(store, source['source_id'], execute=True)
    assert exc.value.code == 'deletion_incomplete'
    assert client.get(f"/generations/{result['request_id']}").status_code == 410
    monkeypatch.setattr(store, '_path', original)
    delete_source(store, source['source_id'], execute=True)
    assert store.repository.pending_deletions() == []


def test_active_source_cannot_be_deleted(tmp_path):
    store = LocalStorage(tmp_path)
    source = store.put_text(TEXT)
    store.repository.claim(str(source.source_id), 'hash', 'simulated')
    with pytest.raises(ExpectedServiceError) as exc:
        delete_source(store, source.source_id, execute=True)
    assert exc.value.code == 'resource_busy'
    assert store.load_source(source.source_id) == source


def test_symlink_deletion_never_follows_external_target(tmp_path):
    store, client, source, result = generated(tmp_path / 'storage')
    preview = delete_source(store, source['source_id'])
    target = store.root / preview['artifacts'][0]
    target.unlink()
    external = tmp_path / 'external'
    external.write_text('must survive')
    target.symlink_to(external)
    with pytest.raises(ExpectedServiceError):
        delete_source(store, source['source_id'], execute=True)
    assert external.read_text() == 'must survive'


def test_events_do_not_contain_content_or_unexpected_exception(tmp_path, caplog):
    secret = 'secret-canary-do-not-log'
    def broken(storage):
        raise RuntimeError(secret)
    with caplog.at_level(logging.INFO, logger='suno_asset.operations'):
        store, client, source, result = generated(tmp_path)
        failed = TestClient(create_app(storage=store, services_factory=broken)).post('/generations',
                    json={'source_id': source['source_id'], 'editorial_focus': secret})
    assert failed.status_code == 500 and secret not in failed.text
    assert secret not in caplog.text and TEXT not in caplog.text
    for record in caplog.records:
        if record.name == 'suno_asset.operations':
            assert 'request_id' in json.loads(record.message)


def test_legacy_json_results_are_imported_without_inventing_audit(tmp_path):
    store, client, source, result = generated(tmp_path)
    # Remove only the new index, preserving an old-format directory of JSON artifacts.
    for suffix in ('', '-wal', '-shm'):
        (tmp_path / ('runtime.sqlite3' + suffix)).unlink(missing_ok=True)
    imported = LocalStorage(tmp_path)
    row = imported.repository.get_run(result['request_id'])
    assert row['configuration']['legacy'] is True
    assert imported.repository.get_events(result['request_id']) == []
    assert imported.repository.attempts_for(result['request_id']) == []
    assert imported.load_execution_result(UUID(result['request_id'])).model_dump(mode='json') == result


def test_deadline_is_checked_between_nodes_and_never_reset(tmp_path):
    store = LocalStorage(tmp_path)
    source = store.put_text(TEXT)
    from suno_asset.services.artifact_lifecycle import runtime_fingerprint
    row, _ = store.repository.claim(str(source.source_id), 'hash', 'simulated',
        configuration={'execution_code_sha256': runtime_fingerprint()})
    with sqlite3.connect(store.repository.path) as conn:
        conn.execute('UPDATE executions SET deadline_at=0 WHERE request_id=?', (row['request_id'],))
    request = GenerationRequest(request_id=UUID(row['request_id']), source=source)
    with pytest.raises(ExpectedServiceError) as exc:
        run_workflow(request, services=replace(fake_services(store), parser=ManualParser()))
    assert exc.value.code == 'execution_deadline'
    assert store.repository.get_run(row['request_id'])['deadline_at'] == 0


def test_real_flow_journals_attempts_and_reports_nominal_cost_without_billing(tmp_path, monkeypatch, caplog):
    from test_m7 import client_with_fake, real_post
    with caplog.at_level(logging.INFO, logger='suno_asset.operations'):
        client, source, session, seen = client_with_fake(tmp_path, monkeypatch)
        response = real_post(client, source['source_id'])
    assert response.status_code == 200 and response.json()['status'] == 'approved'
    result = response.json()
    store = client.app.state.storage
    attempts = store.repository.attempts_for(result['request_id'])
    assert len(attempts) == 4
    assert all(a['status'] == 'confirmed' for a in attempts)
    assert all(json.loads(a['metrics'])['usage_known'] for a in attempts)
    assert all(json.loads(a['metrics'])['nominal_cost_usd'] > 0 for a in attempts)
    events = store.repository.get_events(result['request_id'])
    provider = [e for e in events if e['node'] == 'provider' and e['status'] == 'received']
    assert len(provider) == 4 and all(e['metrics']['authorized_billing_usd'] == 0 for e in provider)
    assert len(list((tmp_path / 'runs').glob('*-attempt-*-request.json'))) == 4
    assert len(list((tmp_path / 'runs').glob('*-attempt-*-response.json'))) == 4
    assert TEXT not in caplog.text and 'test-key' not in caplog.text
    replay = real_post(TestClient(create_app(storage=LocalStorage(tmp_path))), source['source_id'])
    assert replay.json() == result and session.calls == 4


def test_unknown_transport_consumption_is_durable_and_not_replayed(tmp_path, monkeypatch):
    from urllib.error import URLError
    from suno_asset.api import app as app_module
    from suno_asset.services.groq import GroqSession
    calls = []
    def unavailable(*args):
        calls.append(1)
        raise URLError('secret-provider-failure')
    session = GroqSession('test-key', transport=unavailable)
    monkeypatch.setattr(app_module, 'make_session', lambda: session)
    store = LocalStorage(tmp_path)
    client = TestClient(create_app(storage=store))
    source = client.post('/documents', json={'text': TEXT}).json()
    payload = {'source_id': source['source_id'], 'mode': 'real'}
    headers = {'Idempotency-Key': 'unknown-consumption-key'}
    response = client.post('/generations', json=payload, headers=headers)
    assert response.status_code == 200 and response.json()['status'] == 'failed'
    request_id = response.json()['request_id']
    assert store.repository.attempts_for(request_id)[0]['status'] == 'unknown'
    assert response.json()['llm_reserved_tokens'] > 0
    replay = TestClient(create_app(storage=LocalStorage(tmp_path))).post('/generations', json=payload, headers=headers)
    assert replay.json() == response.json() and calls == [1]


def test_orphan_cleanup_requires_proven_dead_writer_and_preserves_unknown_files(tmp_path):
    process = multiprocessing.get_context('spawn').Process(target=_abandoned_write, args=(str(tmp_path),))
    process.start()
    process.join(20)
    assert process.exitcode == 9
    store = LocalStorage(tmp_path)
    unknown = store.root / 'units/unknown-file'
    unknown.write_bytes(b'not managed')
    preview = inspect_orphans(store)
    assert any(r['code'] == 'incomplete_write' and r['writer_alive'] is False for r in preview)
    report = inspect_orphans(store, execute=True)
    assert any(r['removed'] for r in report)
    assert unknown.read_bytes() == b'not managed'


def test_direct_workflow_records_actual_runtime_fingerprint(tmp_path):
    from suno_asset.services.artifact_lifecycle import runtime_fingerprint
    store = LocalStorage(tmp_path)
    source = store.put_text(TEXT)
    request = GenerationRequest(request_id=uuid4(), source=source,
                                resolved_configuration={'execution_code_sha256': 'untrusted-caller-value'})
    services = replace(fake_services(store), parser=ManualParser())
    result = run_workflow(request, services=services)
    expected = runtime_fingerprint()
    assert result.manifest['resolved_configuration']['execution_code_sha256'] == expected
    assert store.repository.get_run(str(request.request_id))['configuration']['execution_code_sha256'] == expected
    events = store.repository.get_events(str(request.request_id))
    assert run_workflow(request, services=services) == result
    assert store.repository.get_events(str(request.request_id)) == events


@pytest.mark.parametrize('code, expected', [('partial_extraction', 'partial_extraction'),
                                         ('secret-arbitrary-service-code', 'service_error')])
def test_failed_node_records_sanitized_error_code(tmp_path, caplog, code, expected):
    store = LocalStorage(tmp_path)
    source = store.put_text(TEXT)
    request = GenerationRequest(request_id=uuid4(), source=source)
    class BrokenParser:
        def parse(self, *args):
            raise ExpectedServiceError(code, 'private document details')
    with caplog.at_level(logging.INFO, logger='suno_asset.operations'):
        result = run_workflow(request, services=replace(fake_services(store), parser=BrokenParser()))
    assert result.status.value == 'failed'
    event = next(e for e in store.repository.get_events(str(request.request_id))
                 if e['node'] == 'parse' and e['status'] == 'failed')
    assert event['metrics']['error_code'] == expected
    assert 'secret-arbitrary-service-code' not in caplog.text and 'private document details' not in caplog.text


def test_direct_real_replay_returns_before_reconfiguring_used_session(tmp_path, monkeypatch):
    from test_m7 import client_with_fake, real_post
    from suno_asset.api.app import _real_services
    from suno_asset.workflow.graph import REAL_POLICY
    client, source, session, seen = client_with_fake(tmp_path, monkeypatch)
    response = real_post(client, source['source_id'])
    assert response.status_code == 200 and response.json()['status'] == 'approved'
    result = response.json()
    store = client.app.state.storage
    request_id = UUID(result['request_id'])
    request = GenerationRequest.model_validate_json(store._read(f'runs/{request_id.hex}-request.json'))
    calls = session.calls
    replay = run_workflow(request, services=_real_services(store, session), policy=REAL_POLICY)
    assert replay.model_dump(mode='json') == result and session.calls == calls


@pytest.mark.parametrize('action', ['delete-source', 'orphans'])
def test_crashed_write_staging_is_owned_and_removed_safely(tmp_path, action):
    process = multiprocessing.get_context('spawn').Process(target=_crash_before_rename, args=(str(tmp_path),))
    process.start()
    process.join(20)
    assert process.exitcode == 11
    store = LocalStorage(tmp_path)
    sources = store.repository.list_sources()
    source_id = sources[0]['id']
    temporary = list((store.root / 'raw').glob('.*.tmp'))
    assert len(temporary) == 1
    unknown = store.root / 'raw/.unrelated.tmp'
    unknown.write_text('unmanaged')
    if action == 'delete-source':
        preview = delete_source(store, source_id)
        assert preview['staging_artifacts'] == [str(temporary[0].relative_to(store.root))]
        delete_source(store, source_id, execute=True)
        assert all(not (store.root / p).exists() for p in preview['artifacts'])
    else:
        inspect_orphans(store, execute=True)
    assert not temporary[0].exists()
    assert unknown.read_text() == 'unmanaged'
