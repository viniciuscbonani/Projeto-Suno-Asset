"""Integridade do gabarito de desenvolvimento; não mede acurácia de modelo."""
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / 'tests' / 'fixtures' / 'evaluation'


def test_initial_m7_gold_is_versioned_traceable_and_balanced() -> None:
    manifest = json.loads((EVALS / 'manifest.json').read_text(encoding='utf-8'))
    rows = json.loads((EVALS / manifest['case_file']).read_text(encoding='utf-8'))
    assert manifest['case_count'] == len(rows) == 36
    assert manifest['source']['sha256'] == 'f03423dc25625aa14ae29c10fdacf7b4665b6714ab0667c0dc86a6f77c43665c'
    assert manifest['source']['physical_pages'] == 6
    assert len({row['id'] for row in rows}) == len(rows)
    assert Counter(row['stage'] for row in rows) == {'pre_generation': 15, 'post_generation': 21}
    assert all(row['dataset_version'] == manifest['dataset_version'] and row['split'] == 'development' for row in rows)
    assert all(row['review'] == {'status': 'agent_draft', 'human_reviewer': None} for row in rows)
    assert all(row['source_id'] == manifest['source']['id'] for row in rows)
    assert all(row['audience'] in {'beginner', 'intermediate', 'advanced'} and row['output_format'] == 'analytical_text' for row in rows)
    assert {row['audience'] for row in rows if row['stage'] == 'pre_generation'} == {'beginner'}
    assert Counter(row['audience'] for row in rows if row['stage'] == 'post_generation') == {
        'beginner': 15, 'intermediate': 3, 'advanced': 3,
    }
    for row in rows:
        assert row['references']
        if isinstance(row['candidate'], str):
            assert row['candidate'].strip()
        else:
            assert row['candidate_scope'] == 'full_output'
            assert all(row['candidate'].get(field) for field in ('title', 'synthesis', 'development', 'implications', 'claims'))
        assert all(ref['fact_id'] in {f'C{i:02}' for i in range(1, 13)} for ref in row['references'])
        assert all(1 <= ref['page'] <= 6 and ref['item'] for ref in row['references'])
        expected = row['expected']
        assert expected['verdict'] in {'passed', 'failed', 'not_verified'}
        assert expected['dimension'] and expected['rationale']
        assert (expected['issue_code'] is None) == (expected['verdict'] == 'passed')
        assert not expected['hard_failure'] or expected['verdict'] == 'failed'
        if row['stage'] == 'post_generation':
            assert row['candidate_scope'] in {'excerpt', 'full_output'}
            assert expected['factual_verdict'] in {'passed', 'failed', 'not_verified'}
            assert expected['audience_verdict'] in {'passed', 'failed', 'not_verified'}
            parts = (expected['factual_verdict'], expected['audience_verdict'])
            combined = 'failed' if 'failed' in parts else 'not_verified' if 'not_verified' in parts else 'passed'
            assert expected['verdict'] == combined
            assert row['required_fact_ids']
            assert set(row['required_fact_ids']) <= {ref['fact_id'] for ref in row['references']}
        else:
            assert 'required_fact_ids' not in row
            assert expected['factual_verdict'] == expected['verdict']
            assert 'audience_verdict' not in expected
    for stage in ('pre_generation', 'post_generation'):
        assert {r['expected']['verdict'] for r in rows if r['stage'] == stage} == {'passed', 'failed', 'not_verified'}


def test_pre_anchor_can_be_technical_but_post_pass_must_be_clear() -> None:
    rows = json.loads((EVALS / 'copom281_m7_development.json').read_text(encoding='utf-8'))
    by_id = {row['id']: row for row in rows}
    assert 'assimetria de alta' in by_id['PRE-07']['candidate']
    assert 'assimetria' not in by_id['POST-07']['candidate']
    assert by_id['POST-07']['expected']['audience_verdict'] == 'passed'
    assert by_id['POST-01']['candidate_scope'] == 'full_output'
    assert by_id['POST-14']['candidate_scope'] == 'full_output'
    assert by_id['POST-14']['expected']['factual_verdict'] == 'passed'
    assert by_id['POST-14']['expected']['audience_verdict'] == 'failed'


def test_other_audiences_have_independent_factual_and_style_cases() -> None:
    rows = json.loads((EVALS / 'copom281_m7_development.json').read_text(encoding='utf-8'))
    for audience in ('intermediate', 'advanced'):
        group = [row for row in rows if row['stage'] == 'post_generation' and row['audience'] == audience]
        assert Counter((row['expected']['factual_verdict'], row['expected']['audience_verdict']) for row in group) == {
            ('passed', 'passed'): 1,
            ('passed', 'failed'): 1,
            ('failed', 'passed'): 1,
        }
        assert sum(row['candidate_scope'] == 'full_output' for row in group) == 2
