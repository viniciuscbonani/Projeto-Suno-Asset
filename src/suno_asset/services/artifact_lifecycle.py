"""Recovery and manual deletion limited to the application's managed storage."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json
import os
import re
from uuid import UUID

from suno_asset.domain.models import SourceRequest, DocumentMetadata, ExecutionResult
from suno_asset.services.protocols import ExpectedServiceError

MANAGED = ('raw', 'sources', 'documents', 'units', 'parse_results', 'chunks', 'evidence', 'runs')


def runtime_fingerprint():
    package = Path(__file__).resolve().parents[1]
    digest = sha256()
    for path in sorted(package.rglob('*.py')):
        digest.update(str(path.relative_to(package)).encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def import_legacy(store):
    """Index valid files only; never manufacture historical events or audit evidence."""
    repo = store.repository
    issues = []
    for folder, model in [('sources', SourceRequest), ('documents', DocumentMetadata)]:
        for path in sorted((store.root / folder).glob('*.json')):
            relative = str(path.relative_to(store.root))
            try:
                content = store._path(relative).read_bytes()
                value = model.model_validate_json(content)
                identity = value.source_id if folder == 'sources' else value.document_id
                if path.stem != identity.hex:
                    raise ValueError('identity mismatch')
                if folder == 'sources':
                    repo.register_source(str(value.source_id))
                else:
                    repo.register_document(str(value.document_id), str(value.source_id))
                if repo.get_artifact(relative) is None:
                    repo.begin_artifact(relative, sha256(content).hexdigest(), len(content), store._owners(relative, content))
                    repo.publish_artifact(relative)
            except Exception:
                # The inspection report includes a controlled path, never raw parser errors.
                issues.append({'path': relative, 'code': 'legacy_unverified'})
    legacy = []
    for path in sorted((store.root / 'runs').glob('*-result.json')):
        relative = str(path.relative_to(store.root))
        try:
            result = ExecutionResult.model_validate_json(store._path(relative).read_bytes())
            if path.name != f'{result.request_id.hex}-result.json':
                raise ValueError('identity mismatch')
            if repo.get_run(str(result.request_id)) is not None:
                continue
            document = repo.get_document(str(result.document_id)) if result.document_id else None
            if document is None:
                raise ValueError('source unknown')
            repo.register_legacy_run(str(result.request_id), document['source_id'], result.mode.value,
                                     configuration={'legacy': True, 'audit_imported': False})
            legacy.append(result)
        except Exception:
            issues.append({'path': relative, 'code': 'legacy_unverified'})
    for folder in MANAGED:
        for path in sorted((store.root / folder).iterdir()):
            if path.name.startswith('.') or not path.is_file():
                continue
            relative = str(path.relative_to(store.root))
            if repo.get_artifact(relative) is not None:
                continue
            try:
                raw = store._path(relative).read_bytes()
                owners = store._owners(relative, raw)
                for kind, owner in owners:
                    repo.ensure_available(kind, owner)
                repo.begin_artifact(relative, sha256(raw).hexdigest(), len(raw), owners)
                repo.publish_artifact(relative)
            except Exception:
                issues.append({'path': relative, 'code': 'legacy_unverified'})
    for result in legacy:
        try:
            paths = [a['path'] for a in repo.artifacts_for('run', str(result.request_id))]
            repo.commit_result(str(result.request_id), f'runs/{result.request_id.hex}-result.json', paths)
        except ExpectedServiceError:
            repo.fail_run(str(result.request_id), 'artifact_unavailable', 503)
    return issues


def _mandatory(store, result, legacy=False):
    paths = [a['path'] for a in store.repository.artifacts_for('run', str(result.request_id))]
    prefix = f'runs/{result.request_id.hex}'
    required = [prefix + '-result.json']
    if not legacy:
        if result.prepared_source:
            required.append(prefix + '-prepared-source.json')
        if result.extraction:
            required.append(prefix + '-extraction.json')
        if result.mode.value == 'real':
            required.append(prefix + '-llm-trace.json')
    store.validate_references(result)
    store.validate_audit(result, legacy=legacy)
    for relative in set(required + paths):
        store._read(relative)
    return sorted(set(required + paths))


def reconcile(store):
    """Reconcile only dead owners; observing a timeout never authorizes replay."""
    repo = store.repository
    repo.reconcile_owners()
    issues = import_legacy(store)
    active = any(r['state'] in {'running', 'registered'} for r in repo.list_runs())
    if not active:
        for artifact in repo.list_artifacts():
            if artifact['state'] == 'pending' and repo.artifact_writer_alive(artifact['path']) is False:
                try:
                    repo.publish_artifact(artifact['path'])
                except ExpectedServiceError:
                    issues.append({'path': artifact['path'], 'code': 'incomplete_write'})
    for run in repo.list_runs():
        if run['state'] not in {'completed', 'interrupted'}:
            continue
        request_id = UUID(run['request_id'])
        relative = f'runs/{request_id.hex}-result.json'
        try:
            result = ExecutionResult.model_validate_json(store._read(relative))
            if result.request_id != request_id:
                raise ValueError('identity mismatch')
            paths = _mandatory(store, result, run['configuration'].get('legacy', False))
            if run['state'] == 'interrupted':
                repo.commit_result(str(request_id), relative, paths)
                repo.event(str(request_id), 'recovery', 'completed')
        except Exception:
            if run['state'] == 'completed':
                repo.fail_run(str(request_id), 'artifact_unavailable', 503)
            issues.append({'path': relative, 'code': 'execution_unavailable'})
    return issues


def staging_for(store, relative):
    """Only our exact temporary namespace for an indexed final path is owned."""
    path = Path(relative)
    parent = store._path(str(path.parent))
    pattern = re.compile(r'\.' + re.escape(path.name) + r'\.[0-9a-f]{32}\.tmp\Z')
    return sorted(str(p.relative_to(store.root)) for p in parent.iterdir() if pattern.fullmatch(p.name))


def delete_run(store, request_id, *, execute=False):
    """Delete one execution; keep its source and artifacts owned by other runs."""
    request_id = str(UUID(str(request_id)))
    repo = store.repository
    repo.reconcile_owners()
    if execute:
        repo.begin_run_deletion(request_id)
    preview = repo.run_deletion_preview(request_id)
    if preview['state'] == 'deleted':
        return preview
    try:
        preview['staging_artifacts'] = sorted({p for relative in preview['artifacts'] for p in staging_for(store, relative)})
        if not execute:
            return preview
        for relative in preview['staging_artifacts']:
            store._path(relative).unlink(missing_ok=True)
        for relative in preview['artifacts']:
            path = store._path(relative)
            path.unlink(missing_ok=True)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            repo.mark_artifact_removed(relative)
        repo.complete_run_deletion(request_id)
    except Exception:
        raise ExpectedServiceError('deletion_incomplete', 'Exclusão parcial; retome a mesma execução.') from None
    return preview | {'state':'deleted'}


def delete_source(store, source_id, *, execute=False):
    source_id = str(UUID(str(source_id)))
    repo = store.repository
    repo.reconcile_owners()
    if not execute:
        preview = repo.deletion_preview(source_id)
        preview['staging_artifacts'] = sorted({p for relative in preview['artifacts'] for p in staging_for(store, relative)})
        return preview
    job = repo.begin_deletion(source_id)
    preview = repo.deletion_preview(source_id)
    try:
        preview['staging_artifacts'] = sorted({p for relative in preview['artifacts'] for p in staging_for(store, relative)})
        for relative in preview['staging_artifacts']:
            store._path(relative).unlink(missing_ok=True)
        for relative in preview['artifacts']:
            path = store._path(relative)
            path.unlink(missing_ok=True)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            repo.mark_artifact_removed(relative)
        repo.complete_deletion(job['id'])
    except Exception:
        repo.fail_deletion(job['id'], 'deletion_incomplete')
        raise ExpectedServiceError('deletion_incomplete', 'Exclusão parcial; retome a mesma fonte.') from None
    return preview | {'state': 'deleted', 'job_id': job['id']}


def inspect_orphans(store, *, execute=False):
    """Unknown files remain untouched; death of a writer must be independently proven."""
    repo = store.repository
    indexed = {a['path']: a for a in repo.list_artifacts()}
    report = []
    for relative, artifact in indexed.items():
        if artifact['state'] != 'pending':
            continue
        writer_alive = repo.artifact_writer_alive(relative)
        removed = False
        recovered = False
        if execute and writer_alive is False and repo.claim_abandoned_artifact(relative):
            temporary_paths = staging_for(store, relative)
            try:
                repo.publish_artifact(relative)
                recovered = True
            except ExpectedServiceError as exc:
                if exc.code != 'artifact_integrity_error':
                    raise
                store._path(relative).unlink(missing_ok=True)
                repo.mark_artifact_removed(relative)
                removed = True
            for temporary in temporary_paths:
                store._path(temporary).unlink(missing_ok=True)
        report.append({'path': relative, 'code': 'incomplete_write', 'writer_alive': writer_alive,
                       'removed': removed, 'recovered': recovered})
    for folder in MANAGED:
        for path in sorted((store.root / folder).iterdir()):
            relative = str(path.relative_to(store.root))
            if path.is_symlink():
                report.append({'path': relative, 'code': 'unsafe_storage_path', 'removed': False})
            elif relative not in indexed:
                report.append({'path': relative, 'code': 'unregistered', 'removed': False})
    return report
