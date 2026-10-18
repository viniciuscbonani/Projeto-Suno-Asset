"""O orçamento e a separação reservada são verificados antes de chamadas pagas."""
import json
from hashlib import sha256
from pathlib import Path
import sys

import pytest

from suno_asset import m7_eval


def test_corpus_and_review_packet_preserve_draft_status_and_hashes():
    directory = Path(__file__).resolve().parents[1] / 'tests/fixtures/evaluation'
    corpus = json.loads((directory / 'corpus.json').read_text())
    form = json.loads((directory / 'human-review-form.json').read_text())
    rows = []
    for source in corpus['sources']:
        assert form['source_hashes'][source['id']] == source['sha256']
        case_file = directory / source['case_file']
        cases = json.loads(case_file.read_text())
        assert sha256(case_file.read_bytes()).hexdigest() == form['dataset_hashes'][source['case_file']]
        assert all(row['source_id'] == source['id'] and row['split'] == source['split'] for row in cases)
        assert all(row['review']['status'] == 'agent_draft' for row in cases)
        assert all(1 <= ref['page'] <= source['physical_pages'] for row in cases for ref in row['references'])
        rows += cases
    assert len(rows) == corpus['case_count'] == 66
    assert len({row['id'] for row in rows}) == len(rows)
    assert sum(row['split'] == 'development' for row in rows) == 51
    assert sum(row['split'] == 'heldout' for row in rows) == 15
    assert {row['id'] for row in form['cases']} == {row['id'] for row in rows}
    assert corpus['human_review_status'] in {'pending', 'reviewed'}
    assert form['status'] in {'pending', 'reviewed'}
    assert all(row['factual_verdict'] in {None, 'passed', 'failed', 'not_verified'} for row in form['cases'])
    if form['status'] == 'reviewed':
        assert form['reviewer'] and form['reviewed_at']
        assert all(row['factual_verdict'] is not None and row['rationale'] for row in form['cases'])


@pytest.mark.parametrize('arguments', [
    ['--allow-heldout'], ['--max-runs', '1'],
    ['--models', 'openai/gpt-oss-20b', 'openai/gpt-oss-20b'],
])
def test_invalid_or_unreviewed_run_never_calls_provider(tmp_path, monkeypatch, arguments):
    directory = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(sys, 'argv', ['m7_eval', '--corpus', str(directory / 'tests/fixtures/evaluation/corpus.json'),
        '--out', str(tmp_path / 'new'), *arguments])
    monkeypatch.setattr(m7_eval, 'make_session', lambda: pytest.fail('não chamar Groq'))
    with pytest.raises(SystemExit) as exc:
        m7_eval.main()
    assert exc.value.code == 2 and not (tmp_path / 'new').exists()


def test_wrong_pdf_hash_never_calls_provider(tmp_path, monkeypatch):
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'%PDF-changed')
    monkeypatch.setattr(m7_eval, 'make_session', lambda: pytest.fail('não chamar Groq'))
    with pytest.raises(ValueError, match='Hash divergente'):
        m7_eval.pipeline_case({'id': 'changed', 'local_pdf': str(pdf), 'sha256': '0' * 64},
                             'openai/gpt-oss-20b', tmp_path, 2)


def test_previous_results_cannot_be_overwritten(tmp_path):
    path = tmp_path / 'result.json'
    m7_eval._save(path, {'status': 'needs_review'})
    with pytest.raises(ValueError, match='sobrescrever'):
        m7_eval._save(path, {'status': 'approved'})
    assert json.loads(path.read_text())['status'] == 'needs_review'


def test_human_review_cannot_be_inferred_from_prepared_form():
    from suno_asset.m7_review import validate_review
    directory = Path(__file__).resolve().parents[1] / 'tests/fixtures/evaluation'
    form = json.loads((directory / 'human-review-form.json').read_text())
    if form['status'] == 'pending':
        with pytest.raises(ValueError, match='humana pendente'):
            validate_review(form, directory)


def test_review_preserves_proposal_and_records_disagreement():
    from suno_asset.m7_review import validate_review
    from suno_asset.services.glossary import GLOSSARY_VERSION
    directory = Path(__file__).resolve().parents[1] / 'tests/fixtures/evaluation'
    form = json.loads((directory / 'human-review-form.json').read_text())
    corpus = json.loads((directory / 'corpus.json').read_text())
    proposals = {row['id']: row for source in corpus['sources']
        for row in json.loads((directory / source['case_file']).read_text())}
    # Dados inteiramente sintéticos no teste, nunca salvos como revisão humana real.
    form.update(reviewer='synthetic-test-fixture', reviewed_at='2026-10-06T12:00:00-03:00', status='reviewed',
        glossary_version=GLOSSARY_VERSION)
    for row in form['cases']:
        proposal = proposals[row['id']]
        row.update(factual_verdict=proposal['expected']['factual_verdict'], rationale='justificativa de teste')
        if proposal['stage'] == 'post_generation': row['audience_verdict'] = proposal['expected']['audience_verdict']
    form['cases'][0]['factual_verdict'] = 'failed'
    for entry in form['glossary']: entry.update(accepted=True, comment='comentário de teste')
    report = validate_review(form, directory)
    assert report['disagreements'] == [form['cases'][0]['id']]
    assert report['cases'][0]['proposed']['factual_verdict'] == 'passed'
    assert report['cases'][0]['factual_verdict'] == 'failed'
    source_id = corpus['sources'][0]['id']
    original_hash = form['source_hashes'][source_id]
    form['source_hashes'][source_id] = '0' * 64
    with pytest.raises(ValueError, match='Fonte alterada'):
        validate_review(form, directory)
    form['source_hashes'][source_id] = original_hash
    form['dataset_hashes'][corpus['sources'][0]['case_file']] = '0' * 64
    with pytest.raises(ValueError, match='alterados'):
        validate_review(form, directory)
