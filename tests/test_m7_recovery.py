"""Recovery proofs use synthetic source/cases and fake transports exclusively."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from suno_asset import m7_cases
from suno_asset.m7_recovery import validate_recovery_lineage, recovery_cost, campaign_admission_cost
from suno_asset.services.groq import GroqSession
from suno_asset.services.quota_ledger import FreeQuotaLedger

CASE = {'id': 'synthetic-C1', 'stage': 'post_generation', 'audience': 'beginner',
    'output_format': 'analytical_text', 'candidate': 'A decisão é 13,75%.',
    'references': [{'fact_id': 'C09', 'page': 5, 'item': '18'}]}
SOURCE = {'id': 'synthetic-source', 'sha256': 'a' * 64}
LABEL = {'factual_verdict': 'passed', 'audience_verdict': 'passed'}
PAGES = {5: '18. A decisão é 13,75%.'}


def response(*_, valid=False):
    content = json.dumps({'factual_verdict': 'passed', 'factual_reason': 'fonte',
        'audience_verdict': 'passed', 'audience_reason': 'claro'}) if valid else 'not-json'
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 10}}


def run(tmp_path, ledger, transport=response):
    session = GroqSession('fake', transport=transport, free_only=True,
        quota_ledger=ledger, quota_evidence={'test': True})
    return m7_cases.run_persistent_case(CASE, SOURCE, PAGES, LABEL,
        'openai/gpt-oss-120b', tmp_path/'batch', 'synthetic-fingerprint', session=session)


def test_terminal_known_failure_can_create_exactly_one_successor_without_erasing_usage(tmp_path):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    failed = run(tmp_path, ledger)
    old_job = ledger.job(failed['quota_job_id'])
    old_result = Path(old_job['result_path']).read_bytes()
    before = ledger.snapshot()
    successor = ledger.create_successor(failed['quota_job_id'], reason='Resposta recebida com JSON inválido')
    assert successor['job_id'] != old_job['job_id'] and successor['status'] == 'queued'
    assert ledger.job(old_job['job_id']) == old_job
    assert Path(old_job['result_path']).read_bytes() == old_result
    assert ledger.snapshot()['reported_tokens'] == before['reported_tokens'] == 20
    assert ledger.create_successor(old_job['job_id'], reason='Resposta recebida com JSON inválido') == successor
    assert ledger.resolve_job(old_job['job_id']) == successor
    with pytest.raises(ValueError, match='justificativa/evidência diferente'):
        ledger.create_successor(old_job['job_id'], reason='Mudar justificativa')


def test_explicit_successor_runs_and_recovers_idempotently_with_portable_lineage(tmp_path):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    failed = run(tmp_path, ledger)
    predecessor_bytes = Path(ledger.job(failed['quota_job_id'])['result_path']).read_bytes()
    successor = ledger.create_successor(failed['quota_job_id'], reason='Repetir após diagnóstico do JSON')
    completed = run(tmp_path, ledger, lambda *_: response(valid=True))
    assert completed['quota_job_id'] == successor['job_id']
    assert completed['quota_root_job_id'] == failed['quota_job_id']
    assert completed['quota_status'] == 'complete'
    assert completed['audit_dir'] != failed['audit_dir']
    assert Path(ledger.job(failed['quota_job_id'])['result_path']).read_bytes() == predecessor_bytes
    validate_recovery_lineage(completed)
    assert recovery_cost(completed) > 0
    assert run(tmp_path, ledger, lambda *_: pytest.fail('idempotent recovery')) == completed
    assert ledger.snapshot()['attempts_24h'] == 2


@pytest.mark.parametrize('status', ['reserved', 'unknown'])
def test_old_uncertain_usage_never_becomes_recoverable_after_window_rollover(tmp_path, status):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    failed = run(tmp_path, ledger)
    with ledger._connect() as db:
        db.execute('UPDATE attempts SET status=?,actual_tokens=NULL,created_at=? WHERE job_id=?',
            (status, (datetime.now(timezone.utc)-timedelta(days=2)).isoformat(), failed['quota_job_id']))
    assert ledger.snapshot()['attempts_24h'] == 0
    with pytest.raises(ValueError, match='incerta'):
        ledger.create_successor(failed['quota_job_id'], reason='Jamais contornar incerteza')
    assert ledger.successor_job(failed['quota_job_id']) is None


def test_running_or_successful_execution_cannot_receive_successor(tmp_path):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    ledger.register_job('running', 'fp', 'synthetic', 'config')
    ledger.start_job('running')
    with pytest.raises(ValueError, match='encerrada'):
        ledger.create_successor('running', reason='Processo ainda pode estar vivo')
    completed = run(tmp_path, ledger, lambda *_: response(valid=True))
    with pytest.raises(ValueError, match='encerrada'):
        ledger.create_successor(completed['quota_job_id'], reason='Sucesso não é recuperação')


@pytest.mark.parametrize('artifact', ['result', 'audit'])
def test_changed_predecessor_artifacts_block_creation_and_portable_acceptance(tmp_path, artifact):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    failed = run(tmp_path, ledger)
    successor = ledger.create_successor(failed['quota_job_id'], reason='Diagnóstico conhecido')
    row = {**failed, 'quota_job_id': successor['job_id'], 'quota_root_job_id': failed['quota_job_id'],
        'quota_recovery_lineage': ledger.recovery_lineage(successor['job_id'])}
    path = Path(ledger.job(failed['quota_job_id'])['result_path']) if artifact == 'result' else Path(next(iter(failed['audit_hashes'])))
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='hash|Auditoria'):
        validate_recovery_lineage(row)
    with pytest.raises(ValueError, match='hash|Auditoria'):
        ledger.resolve_job(failed['quota_job_id'])


def test_terminal_job_cannot_receive_more_provider_reservations(tmp_path):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    failed = run(tmp_path, ledger)
    with pytest.raises(ValueError, match='encerramento'):
        ledger.reserve_attempt(model='openai/gpt-oss-120b', job_id=failed['quota_job_id'],
            payload_sha256='b'*64, tokens=100)


def test_fabricated_known_usage_without_matching_response_or_ledger_is_blocked(tmp_path):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    failed = run(tmp_path, ledger)
    with ledger._connect() as db:
        db.execute('UPDATE attempts SET actual_tokens=1 WHERE job_id=?', (failed['quota_job_id'],))
    with pytest.raises(ValueError, match='Uso, reserva'):
        ledger.create_successor(failed['quota_job_id'], reason='Contadores inconsistentes')


def test_full_ata_successor_keeps_original_result_and_budget_paths(tmp_path, monkeypatch):
    from suno_asset import m7_literal_eval
    from suno_asset.domain.models import ExecutionResult, WorkflowError
    from suno_asset.services.protocols import ExpectedServiceError
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    from io import BytesIO
    from reportlab.pdfgen import canvas
    stream = BytesIO()
    writer = canvas.Canvas(stream)
    writer.drawString(20, 700, 'Synthetic source for recovery')
    writer.save()
    raw = stream.getvalue()
    pdf = tmp_path/'synthetic.pdf'
    pdf.write_bytes(raw)
    source = {'id': 'synthetic-full', 'sha256': sha256(raw).hexdigest(),
        'local_pdf': str(pdf), 'split': 'development'}
    caps = {stage: .06 for stage in ('extract', 'verify', 'generate', 'evaluate', 'refine')}
    protocol = m7_literal_eval.make_execution_protocol('main', 2, caps)
    _, root_id = m7_literal_eval.execution_identity(source, 'literal-quality', 'beginner',
        'analytical_text', 1, 'synthetic-fingerprint', protocol)
    def invocation():
        sess = GroqSession('fake', transport=response, free_only=True,
            quota_ledger=ledger, quota_evidence={'test': True})
        def workflow(request, *, services, policy):
            sess.configure('literal-v1', 'quality')
            try:
                sess.call('extract', 'Synthetic unit test', {}, {'type': 'object'})
            except ExpectedServiceError:
                pass
            return ExecutionResult(request_id=request.request_id, mode='real', status='failed',
                architecture='literal-v1', model_profile='quality', reason='Synthetic terminal failure',
                policy_version=policy.version, manifest=sess.manifest(),
                errors=(WorkflowError(stage='extract', code='invalid_llm_response', message='Synthetic'),))
        monkeypatch.setattr(m7_literal_eval, 'run_workflow', workflow)
        return m7_literal_eval.run_case(source, 'literal-quality', tmp_path/'batch', session=sess,
            quota_job_id=root_id, fingerprint_sha256='synthetic-fingerprint', stage_cost_limits=caps,
            execution_protocol=protocol)
    first = invocation()
    assert first['quota_status'] == 'incomplete'
    original_files = {Path(first[k]): Path(first[k]).read_bytes() for k in ('result_file', 'budget_file')}
    ledger.create_successor(root_id, reason='Falha encerrada de JSON')
    second = invocation()
    assert second['quota_root_job_id'] == root_id and second['quota_job_id'] != root_id
    assert second['result_file'] != first['result_file'] and second['budget_file'] != first['budget_file']
    assert all(path.read_bytes() == data for path, data in original_files.items())
    validate_recovery_lineage(second)


def test_reserve_cases_never_receive_successors_even_when_usage_is_known(tmp_path, monkeypatch):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    monkeypatch.setitem(SOURCE, 'split', 'heldout')
    failed = run(tmp_path, ledger)
    with pytest.raises(ValueError, match='Reserva iniciada'):
        ledger.create_successor(failed['quota_job_id'], reason='Não reutilizar reserva')


def test_admission_sums_predecessors_completed_and_pending_without_alias_duplicates(tmp_path):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    failed = run(tmp_path, ledger)
    successor = ledger.create_successor(failed['quota_job_id'], reason='Encerrada com consumo conhecido')
    pending = campaign_admission_cost(ledger, [failed['quota_job_id'], successor['job_id']], .15)
    assert pending == pytest.approx(.15 + .0000075)
    assert pending > .15
    complete = run(tmp_path, ledger, lambda *_: response(valid=True))
    total = campaign_admission_cost(ledger,
        [failed['quota_job_id'], complete['quota_job_id'], 'not-started'], .15)
    assert total == pytest.approx(.15 + .000015)


def test_removing_lineage_or_changing_its_declared_cost_cannot_hide_failure(tmp_path):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    failed = run(tmp_path, ledger)
    ledger.create_successor(failed['quota_job_id'], reason='Encerrada')
    complete = run(tmp_path, ledger, lambda *_: response(valid=True))
    without = {k:v for k,v in complete.items() if k not in
        {'quota_root_job_id', 'quota_recovery_lineage', 'quota_recovery_nominal_usd'}}
    with pytest.raises(ValueError, match='sem linhagem'):
        validate_recovery_lineage(without)
    with pytest.raises(ValueError, match='Custo declarado'):
        validate_recovery_lineage({**complete, 'quota_recovery_nominal_usd': 0})


def test_admission_keeps_full_unknown_reservation_at_conservative_price(tmp_path):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    ledger.register_job('complete-with-retry', 'fp', 'source', 'configuration')
    ledger.start_job('complete-with-retry')
    row={'attempts':[{'model':'openai/gpt-oss-120b','status':'http_error','usage_known':False,
        'input_estimate':100,'output_reserved':200,'total_reserved':300},
        {'model':'openai/gpt-oss-120b','status':'complete','usage_known':True,
        'prompt_tokens':10,'completion_tokens':10}]}
    path=tmp_path/'completed.json'
    path.write_text(json.dumps(row))
    ledger.complete_job('complete-with-retry', path, sha256(path.read_bytes()).hexdigest())
    assert campaign_admission_cost(ledger,['complete-with-retry'],.15)==pytest.approx(.0001875)
