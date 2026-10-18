"""Selective history cleanup preserves sibling executions and replay tombstones."""
from hashlib import sha256
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest

from suno_asset.api.app import create_app
from suno_asset.services.artifact_lifecycle import delete_run
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.protocols import ExpectedServiceError


def runs(root):
    store = LocalStorage(root)
    source = store.put_text('Fonte compartilhada por duas análises.')
    result = []
    for index in range(2):
        row, _ = store.repository.claim(str(source.source_id), f'hash-{index}', 'simulated', key=f'key-{index}')
        store.save_run_artifact(UUID(row['request_id']), 'note', {'example':index})
        store.repository.fail_run(row['request_id'], 'test_failure')
        result.append(row['request_id'])
    return store, source, result


def test_delete_only_selected_run_and_preserve_shared_artifacts_and_source(tmp_path):
    store, source, (removed, kept) = runs(tmp_path)
    shared = f'runs/{UUID(removed).hex}-shared.json'
    raw = b'{}'
    store.repository.begin_artifact(shared, sha256(raw).hexdigest(), len(raw), [('run',removed), ('run',kept)])
    store._path(shared).write_bytes(raw)
    store.repository.publish_artifact(shared)
    preview = delete_run(store, removed)
    assert preview['shared_artifacts'] == [shared]
    assert store.repository.get_run(removed)['state'] == 'errored'

    assert delete_run(store, removed, execute=True)['state'] == 'deleted'
    assert all(not store._path(path).exists() for path in preview['artifacts'])
    assert store._read(shared) == raw
    assert store.load_source(source.source_id) == source
    assert store._read(f'runs/{UUID(kept).hex}-note.json')
    assert store.repository.get_run(kept)['state'] == 'errored'
    assert delete_run(store, removed, execute=True)['state'] == 'deleted'

    reopened = LocalStorage(tmp_path)
    row, owner = reopened.repository.claim(str(source.source_id), 'hash-0', 'simulated', key='key-0')
    assert not owner and row['state'] == 'deleted'
    app = create_app(storage=reopened)
    listing = next(r.endpoint for r in app.routes if getattr(r,'path',None) == '/generations' and 'GET' in r.methods)
    assert [item['request_id'] for item in listing(50)['items']] == [kept]
    get = next(r.endpoint for r in app.routes if getattr(r,'path',None) == '/generations/{request_id}')
    with pytest.raises(HTTPException) as exc:
        get(UUID(removed))
    assert exc.value.status_code == 410


def test_run_deletion_can_resume_after_partial_failure(tmp_path, monkeypatch):
    store, _, (removed, kept) = runs(tmp_path)
    original = store._path
    def blocked(relative):
        if relative.startswith(f'runs/{UUID(removed).hex}'):
            raise OSError('simulated file failure')
        return original(relative)
    monkeypatch.setattr(store, '_path', blocked)
    with pytest.raises(ExpectedServiceError) as exc:
        delete_run(store, removed, execute=True)
    assert exc.value.code == 'deletion_incomplete'
    assert store.repository.get_run(removed)['state'] == 'deleting'
    assert store.repository.get_run(kept)['state'] == 'errored'
    monkeypatch.setattr(store, '_path', original)
    assert delete_run(store, removed, execute=True)['state'] == 'deleted'


def test_run_deletion_rejects_active_executions_and_writers(tmp_path):
    store = LocalStorage(tmp_path)
    source = store.put_text('Fonte em uso.')
    row, _ = store.repository.claim(str(source.source_id), 'hash', 'simulated')
    rid = row['request_id']
    with pytest.raises(ExpectedServiceError) as exc:
        delete_run(store, rid, execute=True)
    assert exc.value.code == 'resource_busy'
    store.repository.fail_run(rid, 'test_failure')
    store.repository.begin_artifact(f'runs/{UUID(rid).hex}-note.json', sha256(b'{}').hexdigest(), 2, [('run',rid)])
    with pytest.raises(ExpectedServiceError) as exc:
        delete_run(store, rid, execute=True)
    assert exc.value.code == 'resource_busy'
    with pytest.raises(ExpectedServiceError) as exc:
        delete_run(store, uuid4(), execute=True)
    assert exc.value.code == 'resource_not_found'
