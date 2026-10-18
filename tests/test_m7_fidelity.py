"""Offline regressions for literal context and mandatory fidelity verdicts."""
import json
from uuid import uuid4

import pytest

from suno_asset.domain.models import EvaluationReport, GenerationRequest, Importance
from suno_asset.services.literal import (
    LiteralEvaluator, LiteralGenerator, SimulatedLiteralExtractor,
    SimulatedLiteralEvaluator, _Extraction, _numeric_display, consolidate,
    _literal_number_supported, _numeric_field_options,
    validate_extraction,
)
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.source_preparation import prepare_source, source_artifacts
from suno_asset.workflow.graph import LITERAL_POLICY, SIMULATED_POLICY, _no_regression
from suno_asset.services.protocols import ExpectedServiceError
from test_literal_pipeline import Counter, prepared, raw_extraction, session, units


def request(tmp_path):
    return GenerationRequest(request_id=uuid4(), source=LocalStorage(tmp_path).put_text('Fonte offline'),
        audience='beginner', output_format='analytical_text')


def test_projection_title_and_scenario_are_mandatory_literal_support():
    source = prepare_source(units(
        'PROJEÇÕES CONDICIONAIS\n\nNo cenário de referência, a hipótese é Selic constante.\n\n'
        'Tabela 1\nVariação (%)\nÍndice de preços 2026 2027\nIPCA 5,2 3,9\n'
        'Nota: projeções condicionadas à hipótese de câmbio.'), Counter())
    block = next(b for b in source.blocks if any('IPCA 5,2' in e.text
        for e in source.excerpts if e.excerpt_id in b.excerpt_ids))
    raw = raw_extraction(source)
    extraction = consolidate(source, _Extraction.model_validate_json(json.dumps(raw)), source.blocks, 'offline')
    anchor = next(a for a in extraction.anchors if a.owner_block_id == block.block_id)
    assert block.required_context_ids
    assert 'PROJEÇÕES CONDICIONAIS' in anchor.statement
    assert 'Selic constante' in anchor.statement
    assert 'Nota: projeções' in anchor.statement
    originals = {e.excerpt_id:e for e in source.excerpts}
    assert all(e == originals[e.excerpt_id] for e in anchor.support_excerpts)


def test_repeated_projection_titles_are_semantic_headings_and_bare_scenarios_are_kept():
    source = prepare_source(units(
        'PROJEÇÕES CONDICIONAIS\n\nNo cenário de referência, a hipótese é Selic constante.\n\n'
        'Tabela 1\nVariação (%)\nIPCA 5,2 3,9',
        'PROJEÇÕES CONDICIONAIS\n\nTabela 2\nVariação (%)\nIPCA 4,1 3,8'), Counter())
    headings = [b for b in source.blocks if any('PROJEÇÕES CONDICIONAIS' in e.text
        for e in source.excerpts if e.excerpt_id in b.excerpt_ids)]
    assert headings and all(b.kind == 'section' for b in headings)
    first = next(b for b in source.blocks if any('IPCA 5,2' in e.text
        for e in source.excerpts if e.excerpt_id in b.excerpt_ids))
    context = '\n'.join(e.text for e in source.excerpts if e.excerpt_id in first.required_context_ids)
    assert 'PROJEÇÕES CONDICIONAIS' in context and 'Selic constante' in context
    bare = prepared('No cenário de referência, a hipótese é Selic constante.\n\n'
        'Tabela 1\nVariação (%)\nIPCA 5,2 3,9')
    table = next(b for b in bare.blocks if b.kind == 'table')
    assert any('Selic constante' in e.text for e in bare.excerpts if e.excerpt_id in table.required_context_ids)


def test_shared_mandatory_table_support_does_not_deduplicate_distinct_owners():
    source = prepared('PROJEÇÕES\n\nTabela 1\nVariação (%)\nIPCA 5,2 3,9\n'
        'Nota: a hipótese é Selic constante.')
    raw = raw_extraction(source)
    extraction = consolidate(source, _Extraction.model_validate_json(json.dumps(raw)), source.blocks, 'offline')
    validate_extraction(source, extraction)
    assert len(extraction.anchors) == len(source.blocks)
    by_id = {a.anchor_id:a for a in extraction.anchors}
    assert all(by_id[aid].owner_block_id == row.block_id for row in extraction.processing for aid in row.anchor_ids)


def test_table_mandatory_context_stops_at_next_semantic_section():
    source = prepared('PROJEÇÕES CONDICIONAIS\n\nTabela 1\nVariação (%)\nIPCA 5,2 3,9\n\n'
        'REALIZAÇÕES\n\nTabela 2\nVariação (%)\nIPCA 4,1 3,8')
    table = next(b for b in source.blocks if any('IPCA 5,2' in e.text
        for e in source.excerpts if e.excerpt_id in b.excerpt_ids))
    context = '\n'.join(e.text for e in source.excerpts if e.excerpt_id in table.required_context_ids)
    assert 'PROJEÇÕES CONDICIONAIS' in context
    assert 'REALIZAÇÕES' not in context and '4,1 3,8' not in context


def test_currency_note_context_and_display_do_not_mutate_original_quote():
    source = prepared('11. A hipótese de câmbio parte de R$5,15/US$2.\n'
        '12. Outro fato.\n13. Outros riscos.\n2 Valor obtido pela média dos dez dias úteis.\n')
    raw = raw_extraction(source)
    first = raw['blocks'][0]['anchors'][0]
    first['numeric_facts'] = [{'original':'5,15/US$2', 'excerpt_ids':first['excerpt_ids'],
        'indicator':None, 'unit':'R$', 'period':None, 'attribution':None, 'qualifiers':[]}]
    extraction = consolidate(source, _Extraction.model_validate_json(json.dumps(raw)), source.blocks, 'offline')
    anchor = extraction.anchors[0]
    fact = anchor.numeric_facts[0]
    assert fact.original == '5,15/US$2'
    assert _numeric_display(fact.original, anchor.support_excerpts) == '5,15/US$'
    assert 'média dos dez dias úteis' in anchor.statement
    assert set(source.blocks[0].required_context_ids) <= set(fact.excerpt_ids)
    assert _numeric_display('5,15', anchor.support_excerpts) == '5,15'


def test_note_markers_and_ordinals_are_not_financial_quantities():
    texts = ['O câmbio parte de R$5,15/US$2.', '2 Valor obtido pela média dos dez dias úteis.']
    assert not _literal_number_supported('2', texts)
    assert _literal_number_supported('5,15', texts)
    assert not _literal_number_supported('1', ['1.º trimestre de 2028'])
    assert '1.º trimestre de 2028' in _numeric_field_options(['1.º trimestre de 2028'])['period']


def test_inconclusive_or_missing_semantic_dimension_cannot_approve(tmp_path):
    source = prepared('1. Não antecipa cortes caso persistam riscos.')
    extraction = SimulatedLiteralExtractor().extract_prepared(source)
    _, evidence = source_artifacts(source)
    def inconclusive(value, data):
        value['facts'][0]['conditions'] = 'not_verified'
    s = session([], mutate_coverage=inconclusive)
    content = LiteralGenerator(s).generate(request(tmp_path), extraction.anchors, evidence)
    report = LiteralEvaluator(s).evaluate(content, extraction.anchors, evidence, LITERAL_POLICY)
    assert not report.approved and report.critical_facts[0].result.value == 'not_verified'
    def missing(value, data):
        del value['facts'][0]['negations']
    with pytest.raises(ExpectedServiceError):
        LiteralEvaluator(session([], mutate_coverage=missing)).evaluate(content, extraction.anchors, evidence, LITERAL_POLICY)


def test_correct_paraphrase_preserves_negation_and_condition(tmp_path):
    source = prepared('1. Não antecipa cortes enquanto persistirem riscos.')
    extraction = SimulatedLiteralExtractor().extract_prepared(source)
    _, evidence = source_artifacts(source)
    paraphrase = 'O Comitê não prevê cortes de juros enquanto os riscos permanecerem.'
    def write(value, data):
        for section in value.values():
            for block in section:
                block['text'] = paraphrase
    def grade(value, data):
        assert all(c['text'] == paraphrase for c in data['anchors'][0]['candidate_claims'])
        value['facts'][0]['claim_ids'] = [c['id'] for c in data['anchors'][0]['candidate_claims']]
    s = session([], mutate_write=write, mutate_coverage=grade)
    content = LiteralGenerator(s).generate(request(tmp_path), extraction.anchors, evidence)
    assert LiteralEvaluator(s).evaluate(content, extraction.anchors, evidence, LITERAL_POLICY).approved


def test_simulated_mode_rejects_unlinked_material_and_missing_evidence(tmp_path):
    from suno_asset.services.literal import SimulatedLiteralGenerator
    source = prepared('1. Não antecipa cortes enquanto persistirem riscos.')
    extraction = SimulatedLiteralExtractor().extract_prepared(source)
    _, evidence = source_artifacts(source)
    content = SimulatedLiteralGenerator().generate(request(tmp_path), extraction.anchors, evidence)
    evaluator = SimulatedLiteralEvaluator()
    changed = content.model_copy(update={'implications':'A Selic cairá para 5%.'})
    assert not evaluator.evaluate(changed, extraction.anchors, evidence, SIMULATED_POLICY).approved
    missing = content.model_copy(update={'claims':tuple(c.model_copy(update={'evidence_refs':()}) for c in content.claims)})
    assert not evaluator.evaluate(missing, extraction.anchors, evidence, SIMULATED_POLICY).approved


def test_invalid_format_retains_draft_but_spends_no_semantic_calls(tmp_path):
    from suno_asset.services.literal import SimulatedLiteralGenerator
    source = prepared('\n'.join(f'{i}. O Comitê discute o risco {i}.' for i in range(1, 13)))
    extraction = SimulatedLiteralExtractor().extract_prepared(source)
    _, evidence = source_artifacts(source)
    req = request(tmp_path).model_copy(update={'output_format':'informational_carousel'})
    content = SimulatedLiteralGenerator().generate(req, extraction.anchors, evidence)
    s = session([])
    report = LiteralEvaluator(s).evaluate(content, extraction.anchors, evidence, LITERAL_POLICY)
    assert not report.approved and s.calls == 0
    assert [i.code for i in report.issues] == ['format_noncompliant']
    assert report.deferred_due_to_format is True
    legacy=EvaluationReport.model_validate(report.model_dump(exclude={'deferred_due_to_format'}))
    assert legacy.deferred_due_to_format is False
    assert len(report.critical_facts) == len(extraction.anchors)
    assert all(f.result.value == 'not_verified' for f in report.critical_facts)
    assert len(content.cards) == 12


@pytest.mark.parametrize('dimension,source_text,changed', [
    ('modality', '1. A projeção do IPCA é 5,2% em 2027.', 'O IPCA foi 5,2% em 2027.'),
    ('conditions', '1. A hipótese é Selic constante para projetar inflação.', 'A inflação projetada independe da Selic.'),
    ('conditions', '1. O Copom elevará a taxa se os riscos persistirem.', 'O Copom elevará a taxa se os riscos desaparecerem.'),
    ('negations', '1. O Copom não antecipa cortes de juros.', 'O Copom antecipa cortes de juros.'),
    ('attribution', '1. O Focus projeta inflação de 5,2% em 2027.', 'O Copom projeta inflação de 5,2% em 2027.'),
])
def test_explicit_semantic_failure_overrides_false_aggregate_approval(tmp_path, dimension, source_text, changed):
    source = prepared(source_text)
    extraction = SimulatedLiteralExtractor().extract_prepared(source)
    _, evidence = source_artifacts(source)
    def write(value, data):
        for section in value.values():
            for block in section:
                block['text'] = changed
    def grade(value, data):
        # The independent fixture grader records the known source/output pair.
        # An erroneously passed aggregate cannot erase an explicit failed check.
        catalog={e['id']:e['text'] for e in data['source_catalog']}
        assert source_text in '\n'.join(catalog[eid] for eid in data['anchors'][0]['support_excerpt_ids'])
        assert json.dumps(data,ensure_ascii=False).count(source_text)==1
        assert data['anchors'][0]['candidate_claims'][0]['text'] == changed
        for row in value['facts']:
            row.update(result='passed', claim_ids=[c['id'] for c in data['anchors'][0]['candidate_claims']],
                **{dimension:'failed'})
    s = session([], mutate_write=write, mutate_coverage=grade)
    content = LiteralGenerator(s).generate(request(tmp_path), extraction.anchors, evidence)
    report = LiteralEvaluator(s).evaluate(content, extraction.anchors, evidence, LITERAL_POLICY)
    assert not report.approved
    assert report.critical_facts[0].result.value == 'failed'
    assert dimension in report.critical_facts[0].reason


def test_support_importance_does_not_exempt_material_coverage(tmp_path):
    source = prepared('1. O Copom mantém cautela.\n2. Não antecipa cortes enquanto persistirem riscos.')
    extraction = SimulatedLiteralExtractor().extract_prepared(source)
    anchors = (extraction.anchors[0], extraction.anchors[1].model_copy(update={'importance':Importance.SUPPORT}))
    _, evidence = source_artifacts(source)
    def omission(value, data):
        for row, anchor in zip(value['facts'], data['anchors'], strict=True):
            if anchor['importance'] == 'support':
                row.update(result='failed', reason='fato material omitido', claim_ids=[])
    s = session([], mutate_coverage=omission)
    content = LiteralGenerator(s).generate(request(tmp_path), anchors, evidence)
    report = LiteralEvaluator(s).evaluate(content, anchors, evidence, LITERAL_POLICY)
    assert not report.approved and len(report.critical_facts) == 2
    assert report.critical_facts[1].result.value == 'failed'
    passed = SimulatedLiteralEvaluator().evaluate(content, anchors, evidence, SIMULATED_POLICY)
    assert not _no_regression(passed, report)


def test_table_preservation_accepts_union_of_two_exact_claims(tmp_path):
    source = prepared('1. IPCA 5,2% em 2026. IPCA 3,9% em 2027.')
    extraction = SimulatedLiteralExtractor().extract_prepared(source)
    _, evidence = source_artifacts(source)
    def write(value, data):
        anchor_id = data['anchors'][0]['id']
        first = {'text':'1. IPCA 5,2% em 2026.', 'anchor_ids':[anchor_id], 'glossary_term':''}
        second = {'text':'IPCA 3,9% em 2027.', 'anchor_ids':[anchor_id], 'glossary_term':''}
        value.update(synthesis=[first], development=[first, second], implications=[second])
    def union_grade(value, data):
        candidates = data['anchors'][0]['candidate_claims']
        texts = ' '.join(c['text'] for c in candidates)
        assert '2026' in texts and '2027' in texts
        assert not any('2026' in c['text'] and '2027' in c['text'] for c in candidates)
        value['facts'][0].update(claim_ids=[c['id'] for c in candidates])
    s = session([], mutate_write=write, mutate_coverage=union_grade)
    content = LiteralGenerator(s).generate(request(tmp_path), extraction.anchors, evidence)
    report = LiteralEvaluator(s).evaluate(content, extraction.anchors, evidence, LITERAL_POLICY)
    assert report.approved and len(report.critical_facts[0].claim_ids) > 1
    assert SimulatedLiteralEvaluator().evaluate(content, extraction.anchors, evidence, SIMULATED_POLICY).approved
