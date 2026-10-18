"""Explicit note scopes and conservative mandatory source context."""
from uuid import UUID
from suno_asset.domain.models import ParsedUnit, PreparedSource
from suno_asset.services.source_preparation import prepare_source, SEGMENTATION_VERSION


class Counter:
    def count(self, text):
        return len(text.encode()) // 3 + 1


def units(*texts, pages=None):
    return tuple(ParsedUnit(document_id=UUID(int=1), unit_id=f'page-{index}', page=(pages[index - 1] if pages else index),
                            parser_version='test', start=0, end=len(text), text=text)
                 for index, text in enumerate(texts, 1))


def prepared(*texts, **kwargs):
    return prepare_source(units(*texts), Counter(), **kwargs)


def block(source, needle):
    excerpts = {e.excerpt_id: e for e in source.excerpts}
    result = [b for b in source.blocks if any(needle in excerpts[eid].text for eid in b.excerpt_ids)]
    assert len(result) == 1, (needle, result)
    return result[0]


def required(source, owner):
    return '\n'.join(e.text for e in source.excerpts if e.excerpt_id in owner.required_context_ids)


def test_numbered_note_uses_its_explicit_caller_instead_of_nearest_section():
    source = prepared('A) Atualização do cenário do Copom1\n1. A atividade desacelerou.\n'
                      'B) Cenários e riscos\n2. A inflação preocupa.\n'
                      '1 A atualização considera as mudanças desde a reunião anterior.\n')
    note = block(source, '1 A atualização')
    assert 'A) Atualização' in required(source, note)
    assert 'B) Cenários' not in required(source, note)
    assert note.excerpt_ids[0] in block(source, 'A) Atualização').required_context_ids
    assert SEGMENTATION_VERSION == 'logical-source-v1.4'


def test_same_marker_on_different_pages_is_resolved_page_locally():
    source = prepared('A) Cenário local do Copom1\n1. Primeira página.\n1 O primeiro cenário é condicionado.\n',
                      'B) Cenário seguinte do Copom1\n2. Segunda página.\n1 O segundo cenário é observado.\n')
    first = block(source, '1 O primeiro')
    second = block(source, '1 O segundo')
    assert 'A) Cenário' in required(source, first)
    assert 'B) Cenário' not in required(source, first)
    assert 'B) Cenário' in required(source, second)
    assert 'A) Cenário' not in required(source, second)


def test_duplicate_definitions_never_pick_first_and_are_diagnosed():
    source = prepared('1. O cenário do Copom1 é incerto.\n'
                      '1 A primeira definição.\n\n1 A segunda definição.\n')
    owner = block(source, '1. O cenário')
    assert owner.required_context_ids == ()
    assert 'ambiguous_footnote_definition' in owner.diagnostics
    for note in (b for b in source.blocks if b.kind == 'note'):
        assert 'ambiguous_footnote_definition' in note.diagnostics
        assert 'unreferenced_footnote_definition' in note.diagnostics
        assert note.required_context_ids == ()


def test_one_definition_can_have_several_explicit_callers():
    source = prepared('1. O cenário do Copom1 é incerto.\n2. O risco do Copom1 aumentou.\n'
                      '1 A nota se aplica a ambas as menções.\n')
    note = block(source, '1 A nota')
    assert '1. O cenário' in required(source, note)
    assert '2. O risco' in required(source, note)
    assert not note.diagnostics


def test_numbered_note_without_caller_is_pending_not_nearest_section():
    source = prepared('B) Cenários e riscos\n1. A inflação preocupa.\n1 A nota não possui chamada.\n')
    note = block(source, '1 A nota')
    assert note.required_context_ids == ()
    assert 'unreferenced_footnote_definition' in note.diagnostics


def test_reference_without_definition_is_diagnosed():
    source = prepared('1. O cenário do Copom2 é incerto.\n')
    owner = source.blocks[0]
    assert 'unresolved_footnote_reference' in owner.diagnostics
    assert not owner.required_context_ids


def test_definition_on_other_page_is_not_silently_bound():
    source = prepared('1. O cenário do Copom1 é incerto.\n', '1 A definição sem chamada nesta página.\n')
    owner = block(source, '1. O cenário')
    note = block(source, '1 A definição')
    assert not owner.required_context_ids
    assert 'footnote_cross_page_reference_unresolved' in owner.diagnostics
    assert 'unreferenced_footnote_definition' in note.diagnostics


def test_reference_on_second_excerpt_of_cross_page_item_uses_second_page_definition():
    source = prepared('1. O cenário depende da evolução da',
                      'taxa do Copom1 e dos riscos.\n1 A definição se refere à taxa.\n')
    owner = block(source, '1. O cenário')
    note = block(source, '1 A definição')
    assert len(owner.excerpt_ids) == 2
    assert set(owner.excerpt_ids) <= set(note.required_context_ids)
    assert note.excerpt_ids[0] in owner.required_context_ids


def test_page_unavailable_never_resolves_by_first_definition():
    source = prepare_source(units('1. O cenário do Copom1 é incerto.\n1 A nota sem página.\n', pages=[None]), Counter())
    owner = block(source, '1. O cenário')
    note = block(source, '1 A nota')
    assert 'footnote_page_unavailable' in owner.diagnostics
    assert 'footnote_page_unavailable' in note.diagnostics
    assert not owner.required_context_ids
    assert not note.required_context_ids


def test_decimal_and_ordinal_are_not_footnote_calls():
    source = prepared('1. A taxa de R$2,50 é o 1º valor. O indicador IPCA2026 subiu.\n'
                      '2 A nota não foi chamada.\n')
    owner = block(source, '1. A taxa')
    assert not owner.required_context_ids
    assert not owner.diagnostics


def test_currency_footnote_before_comma_remains_bound():
    source = prepared('1. A taxa de R$5,15/US$2, evolui com o cenário.\n'
                      '2 A média considera dez dias úteis.\n')
    owner = block(source, '1. A taxa')
    assert '2 A média' in required(source, owner)
    assert '1. A taxa' in required(source, block(source, '2 A média'))


def test_explicit_table_footnote_carries_title_units_scenario_and_unnumbered_note():
    source = prepared('Tabela 1\nProjeções de inflação no cenário de referência\n'
                      'As projeções são condicionadas à trajetória de juros.\n'
                      'Variação (%)\nÍndice de preços 2026 2027\nIPCA 5,2%1 3,9\n'
                      'Nota: valores condicionais.\n\n1 A nota detalha a hipótese.\n')
    owner = block(source, 'IPCA 5,2%1')
    note = block(source, '1 A nota')
    for context in (required(source, owner), required(source, note)):
        assert 'Tabela 1' in context
        assert 'Projeções de inflação' in context
        assert 'condicionadas à trajetória' in context
        assert 'Nota: valores condicionais.' in context
    assert 'IPCA 5,2%1' in required(source, note)


def test_projection_table_retains_label_before_separate_heading():
    source = prepared('Tabela 1\nProjeções de inflação no cenário de referência\n'
                      'Variação (%)\nÍndice de preços 2026 2027\nIPCA 5,2 3,9\n')
    owner = block(source, 'IPCA 5,2')
    assert 'Tabela 1' in required(source, owner)
    assert 'Projeções de inflação' in required(source, owner)


def test_unnumbered_table_note_and_cross_page_units_remain_available():
    source = prepared('Tabela 1\nVariação (%)\nÍndice de preços 2026 2027',
                      'IPCA 5,2 3,9\nIPCA livres 5,2 4,1\nNota: projeções condicionais.')
    owner = block(source, 'IPCA 5,2')
    context = required(source, owner)
    assert 'Tabela 1' in context and 'Variação (%)' in context
    assert '2026 2027' in context and 'Nota:' in context
    assert 'ambiguous_table_layout' in owner.diagnostics


def test_unreferenced_numbered_note_is_not_added_to_adjacent_table_requirements():
    source = prepared('Tabela 1\nVariação (%)\nIPCA 5,2 3,9\n1 A nota é órfã.\n')
    owner = block(source, 'IPCA 5,2')
    assert '1 A nota' not in required(source, owner)
    assert 'unreferenced_footnote_definition' in block(source, '1 A nota').diagnostics


def test_explicit_reference_cycles_terminate_and_keep_unique_ownership():
    source = prepared('1. O cenário do Copom1 é incerto.\n'
                      '1 A hipótese depende da Nota2.\n2 A condição depende da Nota1.\n')
    owner = block(source, '1. O cenário')
    assert '1 A hipótese' in required(source, owner)
    assert '2 A condição' in required(source, owner)
    for item in source.blocks:
        assert len(item.required_context_ids) == len(set(item.required_context_ids))
        assert not (set(item.excerpt_ids) & set(item.required_context_ids))
    assert len([eid for b in source.blocks for eid in b.excerpt_ids]) == len(source.excerpts)
    assert PreparedSource.model_validate_json(source.model_dump_json()) == source


def test_long_definition_pieces_are_preserved_as_one_definition():
    source = prepared('1. O cenário do Copom1 é incerto.\n1 A primeira condição se mantém. '
                      + 'A outra condição também se mantém. ' * 25, target_tokens=100, max_tokens=2000)
    notes = [b for b in source.blocks if b.kind == 'note']
    assert len(notes) > 1
    assert all('ambiguous_footnote_definition' not in b.diagnostics for b in notes)
    owner = block(source, '1. O cenário')
    assert {eid for b in notes for eid in b.excerpt_ids} <= set(owner.required_context_ids)
    assert all(owner.excerpt_ids[0] in b.required_context_ids for b in notes)



def test_duplicate_definitions_in_same_excerpt_do_not_pick_first():
    source = prepared('1. O cenário do Copom1 é incerto.\n1 A primeira definição.\n1 A segunda definição.\n')
    owner = block(source, '1. O cenário')
    assert not owner.required_context_ids
    assert 'ambiguous_footnote_definition' in owner.diagnostics
    assert 'multiple_footnotes_in_excerpt' in block(source, '1 A primeira').diagnostics


def test_mixed_note_scope_is_explicit_without_losing_table_context():
    source = prepared('Tabela 1\nVariação (%)\nIPCA 5,2%1 3,9\n'
                      'Nota: valores condicionais.\n1 A hipótese explícita.\n')
    note = block(source, '1 A hipótese')
    assert 'mixed_footnote_scope' in note.diagnostics
    assert 'IPCA 5,2%1' in required(source, note)
    assert 'Tabela 1' in required(source, note)
    assert 'Nota: valores condicionais.' in required(source, block(source, 'IPCA 5,2%1'))


def test_time_digits_are_not_calls_even_if_matching_definition_exists():
    source = prepared('1. A reunião iniciou às 10h12 e terminou às 11h44.\n12 A nota sem chamada.\n')
    owner = block(source, '1. A reunião')
    assert not owner.diagnostics
    assert not owner.required_context_ids


def test_short_identifier_or_square_unit_remains_explicitly_ambiguous():
    source = prepared('1. O índice B2 e a área em m2 são distintos.\n2 A hipótese sem chamada inequívoca.\n')
    owner = block(source, '1. O índice')
    assert 'ambiguous_footnote_reference' in owner.diagnostics
    assert not owner.required_context_ids


def test_unresolved_diagnostics_reach_existing_extraction_payload():
    from suno_asset.services.literal import extraction_payload
    source = prepared('1. O cenário do Copom2 é incerto.\n')
    payload = extraction_payload(source, source.blocks)
    assert 'unresolved_footnote_reference' in payload['blocks'][source.blocks[0].block_id]['diagnostics']


def test_table_label_does_not_spread_unrelated_preceding_decision_heading():
    source = prepared('D) Decisão de política monetária\n1. O Copom manteve a taxa.\nTabela 1\n'
                      'Projeções de inflação no cenário de referência\nVariação (%)\nIPCA 5,2 3,9\n')
    owner = block(source, 'IPCA 5,2')
    assert 'Tabela 1' in required(source, owner)
    assert 'Projeções de inflação' in required(source, owner)
    assert 'D) Decisão' not in required(source, owner)


def test_integer_currency_amount_is_not_silently_bound_to_matching_note():
    source = prepared('1. O preço corresponde a US$2 ou R$2.\n2 A nota sem chamada inequívoca.\n')
    owner = block(source, '1. O preço')
    assert not owner.required_context_ids
    assert 'ambiguous_footnote_reference' in owner.diagnostics


def test_alphanumeric_identifier_is_not_silently_bound_to_matching_note():
    source = prepared('1. O indicador XYZ2026a1 é outro índice.\n1 A nota sem chamada inequívoca.\n')
    owner = block(source, '1. O indicador')
    assert not owner.required_context_ids
    assert 'ambiguous_footnote_reference' in owner.diagnostics


def test_required_note_context_over_source_cap_is_rejected_without_trimming():
    from suno_asset.services.groq import GroqSession
    from suno_asset.services.literal import _batches, EXTRACT_PROMPT, extraction_payload, EXTRACTION_SCHEMA, _schema_for
    from suno_asset.services.llm_budget import TokenCounter
    from suno_asset.services.protocols import ExpectedServiceError
    import pytest
    text = ''.join(f'{index}. O cenário do Copom1 é condicional. '
                   + 'A hipótese mantém o horizonte relevante e exige cautela. ' * 20 + '\n'
                   for index in range(1, 13)) + '1 A condição vale para todas as passagens.\n'
    counter = TokenCounter()
    source = prepare_source(units(text), counter)
    note = block(source, '1 A condição')
    ex = {e.excerpt_id: e for e in source.excerpts}
    assert sum(counter.count(ex[eid].text) for eid in note.required_context_ids) > 2000
    original_ids = note.required_context_ids
    session = GroqSession('offline', transport=lambda *_: (_ for _ in ()).throw(AssertionError('provider forbidden')),
                          counter=counter, architecture='literal-v1', profile='quality')
    build = lambda batch: (EXTRACT_PROMPT, extraction_payload(source, batch), _schema_for(EXTRACTION_SCHEMA, source, batch), 2500)
    with pytest.raises(ExpectedServiceError) as exc:
        _batches(session, source, 'extract', build)
    assert exc.value.code == 'llm_request_budget'
    assert note.required_context_ids == original_ids
    assert not session.calls and not session.attempt_records
