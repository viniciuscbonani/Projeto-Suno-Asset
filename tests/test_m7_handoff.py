from hashlib import sha256
from io import BytesIO
import json

import pytest
from reportlab.pdfgen import canvas

from support.source_review import prepare_package
from suno_asset.services.llm_budget import RateGate
from suno_asset.services.provider_limits import limits_from_environment
from suno_asset.services.groq import GroqSession, make_session
from suno_asset.services.protocols import ExpectedServiceError


def corpus_fixture(tmp_path):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(40, 780, '1. O Copom nao promete cortes; caso necessario, ajustara a politica.')
    pdf.save()
    source = tmp_path / 'development.pdf'
    source.write_bytes(buffer.getvalue())
    cases = tmp_path / 'development.json'
    cases.write_text(json.dumps([{'id': 'D01', 'source_id': 'development',
        'split': 'development', 'stage': 'post_generation', 'candidate': 'Nao promete cortes.',
        'expected': {'factual_verdict': 'passed'}, 'review': {'status': 'agent_draft'}}]))
    corpus = tmp_path / 'corpus.json'
    corpus.write_text(json.dumps({'sources': [
        {'id': 'development', 'split': 'development', 'local_pdf': str(source),
         'sha256': sha256(source.read_bytes()).hexdigest(), 'case_file': cases.name},
        # Deliberately nonexistent: preparation must not read heldout inputs.
        {'id': 'heldout', 'split': 'heldout', 'local_pdf': '/absent/reserve.pdf',
         'sha256': '0' * 64, 'case_file': 'absent-reserve.json'}]}))
    return corpus


def test_handoff_source_first_no_reserve_no_prior_verdicts(tmp_path):
    corpus = corpus_fixture(tmp_path)
    out = tmp_path / 'package'
    report = prepare_package(corpus, out)
    assert report['paid'] is False and report['heldout_included'] is False
    assert report['case_count'] == 1 and report['source_ids'] == ['development']
    blind = json.loads((out / 'pass-1/development/cases-blind.json').read_text())
    assert 'expected' not in blind[0] and 'review' not in blind[0]
    inventory = json.loads((out / 'pass-1/development/source-inventory.json').read_text())
    assert inventory['entries'][0]['materiality'] is None
    excerpt = inventory['entries'][0]['source_excerpts'][0]
    pages = json.loads((out / 'pass-1/development/pages.json').read_text())
    assert pages['units'][0]['text'][excerpt['ref']['start']:excerpt['ref']['end']] == excerpt['text']
    matrix = json.loads((out / 'forms/matrix.json').read_text())
    assert len(matrix['rows']) == 9 and all(r['verdict'] is None for r in matrix['rows'])
    assert any(r['output_format'] == 'informational_carousel' for r in matrix['rows'])
    for path, digest in report['package_hashes'].items():
        assert sha256((out / path).read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match='pasta nova'):
        prepare_package(corpus, out)


def test_handoff_rejects_reserve_runs_before_reading_results(tmp_path):
    corpus = corpus_fixture(tmp_path)
    summary = tmp_path / 'summary.json'
    summary.write_text(json.dumps({'rows': [{'source_id': 'heldout', 'result_file': '/absent'}]}))
    with pytest.raises(ValueError, match='reserva'):
        prepare_package(corpus, tmp_path / 'package', (summary,))
    assert not (tmp_path / 'package').exists()


def test_handoff_checks_source_hash(tmp_path):
    corpus = corpus_fixture(tmp_path)
    data = json.loads(corpus.read_text())
    data['sources'][0]['sha256'] = '0' * 64
    corpus.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Hash da fonte'):
        prepare_package(corpus, tmp_path / 'package')


def test_handoff_includes_independent_refinement_before_after_and_audit(tmp_path):
    corpus = corpus_fixture(tmp_path)
    source_hash = json.loads(corpus.read_text())['sources'][0]['sha256']
    audit = tmp_path / 'audit'
    audit.mkdir()
    (audit / 'attempt-001-started.json').write_text('{}')
    summary = tmp_path / 'summary.json'
    summary.write_text(json.dumps({'paid': True, 'rows': [{'source_id': 'development',
        'source_sha256': source_hash, 'audit_dir': str(audit),
        'before': {'revision': 0, 'audience': 'beginner', 'output_format': 'analytical_text'},
        'after': {'revision': 1, 'audience': 'beginner', 'output_format': 'analytical_text'},
        'after_evaluation': {'approved': True}}]}))
    out = tmp_path / 'package'
    report = prepare_package(corpus, out, (summary,))
    assert report['revision_count'] == 2
    assert (out / 'pass-1/development/run-01-001-before.json').exists()
    assert (out / 'pass-1/development/run-01-001-after.json').exists()
    assert (out / 'pass-2/run-01-001/call-audit/attempt-001-started.json').exists()
    assert 'approved' not in (out / 'pass-1/development/run-01-001-after.json').read_text()
    data = json.loads(summary.read_text());data['rows'][0]['source_sha256'] = '0' * 64
    summary.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Hash da fonte da rodada'):
        prepare_package(corpus, tmp_path / 'bad-package', (summary,))


def test_minute_and_day_limits_are_independent(monkeypatch):
    for name in ('GROQ_TPM_LIMIT', 'GROQ_RPM_LIMIT', 'GROQ_RPD_LIMIT', 'GROQ_TPD_LIMIT'):
        monkeypatch.delenv(name, raising=False)
    limits = limits_from_environment()
    assert limits.tokens_per_minute == 8000
    assert limits.requests_per_day is None and limits.tokens_per_day is None
    assert limits.as_dict()['retention'] == 'unknown'
    monkeypatch.setenv('GROQ_RPD_LIMIT', '900')
    monkeypatch.setenv('GROQ_TPD_LIMIT', '240000')
    session = make_session('offline-key', transport=lambda *_: {})
    assert session.tpm_limit == 8000
    assert session.manifest()['provider_limits']['day'] == {'requests': 900, 'tokens': 240000}
    monkeypatch.setenv('GROQ_RPM_LIMIT', '0')
    with pytest.raises(ExpectedServiceError, match='Cotas'):
        limits_from_environment()


def test_shared_daily_guard_counts_unknown_consumption_and_blocks_transport():
    gate = RateGate(enforce_window=False)
    gate.daily_reservations.append((__import__('time').monotonic(), 100))
    sent = []
    session = GroqSession('offline-key', transport=lambda *args: sent.append(args),
        rate_gate=gate, rpd_limit=1)
    with pytest.raises(ExpectedServiceError) as exc:
        session.call('verify', 'Instrucoes', {}, {'type': 'object'}, 1)
    assert exc.value.code == 'llm_daily_budget'
    assert not sent and session.calls == 0
    assert gate.delay(1, 8000, 0, 2) == 0


def test_daily_tokens_and_minute_request_window():
    gate = RateGate()
    gate.daily_reservations = [(100, 1000)]
    with pytest.raises(ExpectedServiceError):
        gate.check_daily(1, 120, None, 1000)
    gate.check_daily(1000, 86501, 1, 1000)
    gate.reservations = [(100, 10)]
    assert gate.delay(1, 8000, 101, 1) > 59


def test_reported_underflow_updates_both_shared_windows():
    schema = {'type': 'object'}
    gate = RateGate(enforce_window=False)
    session = GroqSession('offline-key', rate_gate=gate, transport=lambda *_: {
        'usage': {'prompt_tokens': 1000, 'completion_tokens': 1},
        'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]})
    reserved = session.budget('verify', 'Instrucoes', {}, schema, 1).total
    assert reserved < 1001
    with pytest.raises(ExpectedServiceError) as exc:
        session.call('verify', 'Instrucoes', {}, schema, 1)
    assert exc.value.code == 'token_estimate_underflow'
    assert session.reserved_tokens == 1001
    assert gate.reservations[0][1] == gate.daily_reservations[0][1] == 1001
    with pytest.raises(ExpectedServiceError) as daily:
        gate.check_daily(1, __import__('time').monotonic(), None, 1001)
    assert daily.value.code == 'llm_daily_budget'
