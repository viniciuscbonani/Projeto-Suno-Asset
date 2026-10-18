"""Offline literal contracts: adversarial associations, complete scopes and API."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from suno_asset.api.app import _real_services,create_app
from suno_asset.domain.models import (Anchor, Claim, CriticalFactPreservation, EvaluationCheck,
    EvaluationReport, ExecutionResult, GeneratedContent, GenerationRequest, ParsedUnit, PreparedSource)
from suno_asset.services.groq import GroqSession
from suno_asset.services.literal import (LiteralExtractor,LiteralFactVerifier,LiteralGenerator,LiteralEvaluator,
    SimulatedLiteralExtractor,_Extraction,consolidate,validate_extraction)
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.source_preparation import prepare_source,source_artifacts
from suno_asset.workflow.graph import LITERAL_POLICY,_no_regression,run_workflow
from suno_asset.m7_literal_eval import cost_records,configuration,CONFIGURATIONS


class Counter:
    mode='offline-fixture'
    def count(self,text):return len(text.encode())//3+1
    def estimate(self,payload):return self.count(json.dumps(payload,ensure_ascii=False))+128


def units(*texts):
    doc=uuid4()
    return tuple(ParsedUnit(document_id=doc,unit_id=f'page-{i}',page=i,parser_version='test',
        start=0,end=len(text),text=text) for i,text in enumerate(texts,1))


def prepared(text='1. O Copom decidiu reduzir a Selic para 13,75% ao ano.\n\n2. Caso necessário, manterá a restrição; não antecipa cortes.'):
    return prepare_source(units(text),Counter())


def raw_extraction(source):
    return {'blocks':[{'block_id':b.block_id,'classification':'material','reason':'fato literal',
        'anchors':[{'kind':'rationale','importance':'critical','excerpt_ids':list(b.excerpt_ids),'numeric_facts':[]}]}
        for b in source.blocks]}


def response(value,finish='stop'):
    return {'choices':[{'finish_reason':finish,'message':{'content':json.dumps(value)}}],
        'usage':{'prompt_tokens':10,'completion_tokens':10}}


def quoted_anchor(payload, anchor):
    catalog={row['id']:row['text'] for row in payload['excerpts']}
    return '\n'.join(catalog[eid] for eid in anchor['excerpt_ids'])


def transport(seen,*,mutate_extract=None,mutate_verify=None,mutate_write=None,mutate_coverage=None,
              mutate_source=None,fail_stage=None,truncated=False):
    def send(payload,*_):
        stage=payload['response_format']['json_schema']['name'].split('_')[1]
        data=json.loads(payload['messages'][1]['content']);seen.append((stage,payload['model'],data))
        assert payload['reasoning_effort']=='low'
        if fail_stage==stage:
            from urllib.error import URLError
            raise URLError('offline failure')
        if stage=='extract':
            value={'blocks':[{'block_id':b['block_id'],'classification':'material','reason':'fato literal',
                'anchors':[{'kind':'rationale','importance':'critical','excerpt_ids':b['excerpt_ids'],'numeric_facts':[]}]}
                for bid,row in data['blocks'].items() for b in ({'block_id':bid,**row},)]}
            if mutate_extract:mutate_extract(value,data)
            for row in value['blocks']:
                for anchor in row['anchors'] or []:
                    facts=[]
                    for fact in anchor['numeric_facts']:
                        if 'indicator_id' in fact:
                            facts.append(fact);continue
                        def selected(value,field):
                            if value is None:return None
                            prefix={'indicator':'I','unit':'U','period':'P','attribution':'A','qualifiers':'Q'}[field]
                            candidates=[selector for selector,text in data['numeric_metadata_catalog'].items()
                                if selector.rsplit('-',1)[-1].startswith(prefix) and ' '.join(text.split())==' '.join(value.split())]
                            return candidates[0] if candidates else 'UNKNOWN_SELECTOR'
                        facts.append({'original':fact['original'],'excerpt_ids':fact['excerpt_ids'],
                            **{field+'_id':selected(fact[field],field) for field in ('indicator','unit','period','attribution')},
                            'qualifier_ids':[selected(value,'qualifiers') for value in fact['qualifiers']]})
                    anchor['numeric_facts']=facts
            value['blocks'] = {row['block_id']: {k:(None if k == 'anchors' and not v else v)
                                              for k,v in row.items() if k != 'block_id'}
                               for row in value['blocks']}
            return response(value,finish='length' if truncated else 'stop')
        if stage=='verify':
            if data.get('control')=='source_materiality':
                value={'blocks':[{'block_id':block['block_id'],
                    'classification':'administrative' if block['kind'] in {'header','footer','section'} else 'material',
                    'material_excerpt_ids':[] if block['kind'] in {'header','footer','section'} else block['excerpt_ids'],
                    'reason':'source-only synthetic fixture'} for block in data['blocks']]}
                if mutate_source:mutate_source(value,data)
                return response(value)
            value={'blocks':[{'block_id':b['block_id'],'support':'passed','coverage':'passed','relevance':'passed',
                'reason':'fonte e classificação conferidas','anchors':[{'anchor_id':aid,'result':'passed','reason':'fonte confere'} for aid in b['anchor_ids']],
                'omission_excerpt_ids':[]} for b in data['processing']]}
            if mutate_verify:mutate_verify(value,data)
        elif stage in {'generate','refine'}:
            catalog={e['id']:e['text'] for e in data['source_catalog']}
            blocks=[{'text':'\n'.join(catalog[eid] for eid in a['support_excerpt_ids']),
                'anchor_ids':[a['id']],'glossary_term':''} for a in data['anchors']]
            value={'synthesis':blocks[:1],'development':blocks,'implications':blocks[-1:]}
            if mutate_write:mutate_write(value,data)
        elif 'claims' in data and 'anchors' not in data:
            value={'claims':[{'claim_id':c['claim_id'],'support':'passed','numbers':'passed','citations':'passed','reason':'suporte conferido'} for c in data['claims']]}
        elif 'anchors' in data:
            value={'facts':[{'anchor_id':a['id'],'result':'passed','reason':'conteúdo integral preservado',
                'conditions':'passed','negations':'passed','modality':'passed','attribution':'passed',
                'claim_ids':[c['id'] for c in a['candidate_claims'] if any(
                    data_excerpt['text'] in c['text'] for data_excerpt in data['source_catalog']
                    if data_excerpt['id'] in a['support_excerpt_ids'])][:1]}
                for a in data['anchors']]}
            if mutate_coverage:mutate_coverage(value,data)
        else:value={'coherence':'passed','adequacy':'passed','reason':'coerência e público conferidos'}
        return response(value)
    return send


def session(seen,**kwargs):return GroqSession('fake',transport=transport(seen,**kwargs),counter=Counter())


def test_page_continuation_keeps_individual_offsets_and_one_owner():
    source=prepare_source(units('1. O Comitê não reagirá aos efeitos primários,','caso não se materializem efeitos de segunda ordem.\n2. Outros riscos.'),Counter())
    first=source.blocks[0]
    assert len(first.excerpt_ids)==2
    assert [e.ref.page for e in source.excerpts[:2]]==[1,2]
    assert 'cross_page_continuation' in first.diagnostics
    assert len([eid for b in source.blocks for eid in b.excerpt_ids])==len(source.excerpts)
    assert PreparedSource.model_validate_json(source.model_dump_json())==source


def test_wrapped_footnote_does_not_interrupt_cross_page_item():
    source = prepare_source(units(
        '281ª Reunião\nCOPOM\n15 e 16 de setembro\n'
        '8. A política fiscal afeta o prêmio a termo da\n'
        '1 A nota descreve mudanças desde a\nreunião anterior.\n3 bcb.gov.br',
        '281ª Reunião\nCOPOM\n15 e 16 de setembro\n'
        'curva de juros. Políticas devem ser previsíveis.\n9. Outro fato.'), Counter())
    item = next(b for b in source.blocks if any(e.text.startswith('8.')
        for e in source.excerpts if e.excerpt_id in b.excerpt_ids))
    support = [e for e in source.excerpts if e.excerpt_id in item.excerpt_ids]
    assert [e.ref.page for e in support] == [1, 2]
    assert 'cross_page_continuation' in item.diagnostics
    assert all('nota descreve' not in e.text for e in support)
    note = next(b for b in source.blocks if b.kind == 'note')
    assert 'reunião anterior.' in next(e.text for e in source.excerpts if e.excerpt_id in note.excerpt_ids)


def test_inline_table_reference_keeps_narrative_item_intact():
    source = prepared('3. A projeção está no cenário de referência\nrelevante para a política monetária (Tabela 1).')
    assert len(source.blocks) == 1
    assert source.blocks[0].kind == 'item'


def test_currency_footnote_before_punctuation_is_context_not_a_decimal():
    source = prepared('11. O câmbio parte de R$5,15/US$2, evoluindo segundo a PPC.\n'
        '12. Outro cenário.\n13. Outros riscos.\n'
        '2 Valor obtido pela média dos dez dias úteis.\n')
    item = source.blocks[0]
    context = '\n'.join(e.text for e in source.excerpts if e.excerpt_id in item.context_ids)
    assert '2 Valor obtido pela média dos dez dias úteis.' in context
    decimal = prepared('11. Valor R$2,50 no cenário.\n12. Outro cenário.\n13. Outros riscos.\n'
        '2 Nota que não é referenciada pelo valor decimal.\n')
    assert not any('2 Nota' in e.text for e in decimal.excerpts
        if e.excerpt_id in decimal.blocks[0].context_ids)


def test_table_header_unit_note_and_rows_across_pages_are_attached():
    source=prepare_source(units('Tabela 1\nVariação (%)\nÍndice de preços 2026 2027',
        'IPCA 5,2 3,9\nIPCA livres 5,2 4,1\nNota: projeções condicionais.'),Counter())
    for block in source.blocks:
        if block.kind=='table' and any('IPCA 5,2' in e.text for e in source.excerpts if e.excerpt_id in block.excerpt_ids):
            text='\n'.join(e.text for e in source.excerpts if e.excerpt_id in (*block.excerpt_ids,*block.context_ids))
            assert 'Variação (%)' in text and '2026 2027' in text and 'Nota:' in text
            assert 'ambiguous_table_layout' in block.diagnostics


def test_long_blocks_split_only_at_sentences_and_unrecognized_text_is_diagnosed():
    text='Um fato qualitativo sobre riscos persistentes. '*120
    source=prepare_source(units(text),Counter(),target_tokens=180,max_tokens=2000)
    assert len(source.blocks)>1
    assert ''.join(e.text for e in source.excerpts)==text
    assert 'unrecognized_structure' in source.diagnostics
    with pytest.raises(ExpectedServiceError,match='indivisível'):
        prepare_source(units('a'*9000),Counter())


def test_prepared_source_rejects_omissions_and_altered_quotes():
    source=prepared()
    for bad in (source.model_copy(update={'blocks':source.blocks[:-1]}),
                source.model_copy(update={'excerpts':(source.excerpts[0].model_copy(update={'text':'x'*len(source.excerpts[0].text)}),*source.excerpts[1:])})):
        with pytest.raises(ValidationError):PreparedSource.model_validate(bad.model_dump())


@pytest.mark.parametrize('case',['invented','reference','substring','unit','indicator','period','attribution','qualifier','footnote'])
def test_numeric_fields_require_exact_local_support(case):
    source=prepared('1. Selic: 13,75% em 2026. IPCA: 5,2% em 2027. Cotação R$5,50/US$¹.')
    raw=raw_extraction(source);anchor=raw['blocks'][0]['anchors'][0];eid=anchor['excerpt_ids'][0]
    fact={'original':'13,75%','excerpt_ids':[eid],'indicator':'Selic','unit':'%','period':'2026','attribution':None,'qualifiers':[]}
    edits={'invented':{'original':'99,99%'},'reference':{'excerpt_ids':['MISSING']},'substring':{'original':'3,75%'},
        'unit':{'unit':'p.p.'},'indicator':{'indicator':'PIB'},'period':{'period':'2030'},'attribution':{'attribution':'Focus'},
        'qualifier':{'qualifiers':['garantido']},'footnote':{'original':'1'}}
    fact.update(edits[case]);anchor['numeric_facts']=[fact]
    with pytest.raises(ExpectedServiceError):consolidate(source,_Extraction.model_validate_json(json.dumps(raw)),source.blocks,'test')


def test_same_rate_and_different_periods_are_not_deduplicated():
    source=prepared('1. Selic 5,2% em 2026. IPCA 5,2% em 2027.')
    raw=raw_extraction(source);a=raw['blocks'][0]['anchors'][0]
    b=deepcopy(a)
    for anchor,indicator,period in [(a,'Selic','2026'),(b,'IPCA','2027')]:
        anchor['numeric_facts']=[{'original':'5,2%','excerpt_ids':anchor['excerpt_ids'],'indicator':indicator,'unit':'%',
            'period':period,'attribution':None,'qualifiers':[]}]
    raw['blocks'][0]['anchors']=[a,deepcopy(a),b]
    extraction=consolidate(source,_Extraction.model_validate_json(json.dumps(raw)),source.blocks,'test')
    assert len(extraction.anchors)==2
    assert {f.period for a in extraction.anchors for f in a.numeric_facts}=={'2026','2027'}


def test_context_has_no_extraction_owner_and_missing_or_duplicate_batches_fail():
    source=prepared();raw=raw_extraction(source)
    raw['blocks'][0]['anchors'][0]['excerpt_ids']=list(source.blocks[0].context_ids)
    with pytest.raises(ExpectedServiceError,match='contexto'):consolidate(source,_Extraction.model_validate_json(json.dumps(raw)),source.blocks,'test')
    extraction=SimulatedLiteralExtractor().extract_prepared(source)
    for processing in (extraction.processing[:-1],(*extraction.processing,extraction.processing[0])):
        with pytest.raises(ExpectedServiceError):validate_extraction(source,extraction.model_copy(update={'processing':processing}))


def test_extraction_can_be_empty_for_administrative_blocks_and_is_still_verified():
    source=prepared('Participantes\nJosé da Silva')
    def empty(value,data):
        for b in value['blocks']:b.update(classification='administrative',reason='lista de participantes sem votação',anchors=[])
    def administrative(value,data):
        for b in value['blocks']:b.update(classification='administrative',material_excerpt_ids=[],reason='Lista de participantes sem votação.')
    seen=[];s=session(seen,mutate_extract=empty,mutate_source=administrative)
    extraction=LiteralExtractor(s).extract_prepared(source)
    assert not extraction.anchors and all(r.classification=='administrative' for r in extraction.processing)
    report=LiteralFactVerifier(s).verify_prepared(source,extraction,LITERAL_POLICY)
    assert report.processing_complete and len(report.block_verifications)==len(source.blocks)
    assert not report.approved # A document with no critical facts cannot be written.


def test_economy_compact_verification_skips_only_the_separate_materiality_pass():
    source=prepared()
    extraction=SimulatedLiteralExtractor().extract_prepared(source)
    seen=[]
    report=LiteralFactVerifier(session(seen)).verify_prepared(
        source,extraction,LITERAL_POLICY.model_copy(update={'version':'mvp-efficient-v1'}),compact=True)
    assert report.processing_complete and report.approved
    assert report.source_materiality_complete is None and not report.source_materiality_checks
    assert any(check.name=='source_materiality' and not check.applicable for check in report.checks)
    assert seen and all(stage=='verify' and data.get('control')!='source_materiality' for stage,_,data in seen)


@pytest.mark.parametrize('dimension',['support','coverage','relevance','anchor'])
def test_semantic_uncertainty_or_omission_blocks_writing(dimension,tmp_path):
    def invalidate(value,data):
        row=value['blocks'][0]
        if dimension=='anchor':row['anchors'][0]['result']='not_verified'
        else:row[dimension]='not_verified'
    s=session([],mutate_verify=invalidate)
    store=LocalStorage(tmp_path)
    request=GenerationRequest(request_id=uuid4(),source=store.put_text('1. O Copom não antecipa cortes se persistirem riscos.'),
        mode='real',architecture='literal-v1',model_profile='quality',audience='beginner',output_format='analytical_text')
    result=run_workflow(request,services=_real_services(store,s),policy=LITERAL_POLICY)
    assert result.status.value=='failed' and result.content is None
    assert all(call['stage']!='generate' for call in s.records)


@pytest.mark.parametrize('case',['swapped_period','same_number_other_indicator','percentage_points','condition','negation','qualitative','material_discard'])
def test_semantic_verifier_receives_structured_fields_and_original_context(case):
    source=prepared('1. Selic 5,2% em 2026. IPCA 5,2% em 2027. Não haverá cortes se os riscos persistirem.')
    seen=[]
    def mutate(value,data):
        row=value['blocks'][0];row['support']='failed';row['relevance']='failed'
        row['anchors'][0].update(result='failed',reason=case)
    s=session(seen,mutate_verify=mutate)
    extraction=LiteralExtractor(s).extract_prepared(source)
    report=LiteralFactVerifier(s).verify_prepared(source,extraction,LITERAL_POLICY)
    assert not report.approved and case in report.block_verifications[0].anchors[0].reason
    payload=next(data for stage,_,data in seen if stage=='verify' and 'processing' in data)
    assert payload['excerpts'][0]['text']==source.excerpts[0].text
    assert payload['processing'][0]['classification']=='material'
    assert 'statement' not in payload['anchors'][0]
    assert quoted_anchor(payload,payload['anchors'][0])==extraction.anchors[0].statement
    assert len(payload['excerpts'])==len({row['id'] for row in payload['excerpts']})
    assert payload['anchors'][0]['owner_block_id']==source.blocks[0].block_id


def test_qualitative_and_empty_administrative_blocks_remain_subject_to_verification():
    source=prepared('1. Não antecipa cortes se persistirem riscos.\n5 bcb.gov.br')
    raw=raw_extraction(source)
    raw['blocks'][-1].update(classification='administrative',anchors=[])
    extraction=consolidate(source,_Extraction.model_validate_json(json.dumps(raw)),source.blocks,'test')
    seen=[]
    def uncertain(value, data):
        row=next(r for r in value['blocks'] if not r['anchors'])
        row['support']='not_verified'
    report=LiteralFactVerifier(session(seen,mutate_verify=uncertain)).verify_prepared(source,extraction,LITERAL_POLICY)
    assert not report.approved
    payload=next(data for stage,_,data in seen if stage=='verify' and 'processing' in data)
    assert 'statement' not in payload['anchors'][0]
    assert quoted_anchor(payload,payload['anchors'][0])=='1. Não antecipa cortes se persistirem riscos.\n'
    assert payload['anchors'][0]['numeric_facts']==[]
    assert any(r['classification']=='administrative' and not r['anchor_ids'] for r in payload['processing'])


@pytest.mark.parametrize('missing',['block','anchor','duplicate','unknown_omission'])
def test_incomplete_block_verification_is_never_approved(missing):
    source=prepared();extraction=SimulatedLiteralExtractor().extract_prepared(source)
    def invalidate(value,data):
        if missing=='block':value['blocks'].pop()
        elif missing=='anchor':value['blocks'][0]['anchors'].pop()
        elif missing=='duplicate':value['blocks'].append(value['blocks'][0])
        else:value['blocks'][0].update(coverage='failed',omission_excerpt_ids=['UNKNOWN'])
    with pytest.raises(ExpectedServiceError):LiteralFactVerifier(session([],mutate_verify=invalidate)).verify_prepared(source,extraction,LITERAL_POLICY)


def test_one_correction_uses_source_and_errors_and_persistent_error_blocks():
    source=prepared();seen=[]
    def bad(value,data):
        if 'errors' not in data:value['blocks'][0]['anchors'][0]['excerpt_ids']=['UNKNOWN']
    s=session(seen,mutate_extract=bad)
    result=LiteralExtractor(s).extract_prepared(source)
    assert len(result.corrections)==1
    extract_seen=[item for item in seen if item[0]=='extract']
    assert extract_seen[0][2]['excerpts']==extract_seen[1][2]['excerpts']
    assert extract_seen[1][2]['errors'][0]['code']=='invalid_excerpt_reference'
    assert 'previous_extraction' not in extract_seen[1][2]
    assert result.corrections[0]['before'] is not None
    s=session([],mutate_extract=lambda v,d:v['blocks'][0]['anchors'][0].update(excerpt_ids=['UNKNOWN']))
    with pytest.raises(ExpectedServiceError,match='não corrigida'):LiteralExtractor(s).extract_prepared(source)
    assert s.calls==3
    s=session([],truncated=True)
    with pytest.raises(ExpectedServiceError):LiteralExtractor(s).extract_prepared(source)
    assert s.calls==3


def test_extraction_repair_preserves_valid_critical_blocks_outside_the_error():
    source=prepared('1. Primeiro fato.\n2. Não reage a efeitos primários; reage se os efeitos secundários se materializarem.')
    seen=[]
    def mutate(value, data):
        if 'errors' not in data:
            value['blocks'][0]['anchors'][0]['excerpt_ids']=['UNKNOWN']
        else:
            # A whole-batch repair would discard this already valid critical row.
            for row in value['blocks']:
                if row['block_id']==source.blocks[1].block_id:
                    row.update(classification='administrative',anchors=[])
    result=LiteralExtractor(session(seen,mutate_extract=mutate)).extract_prepared(source)
    extract_seen=[item for item in seen if item[0]=='extract']
    assert list(extract_seen[1][2]['blocks'])==[source.blocks[0].block_id]
    assert result.corrections[0]['preserved_block_ids']==[source.blocks[1].block_id]
    anchor=next(a for a in result.anchors if a.owner_block_id==source.blocks[1].block_id)
    assert anchor.importance.value=='critical'
    assert 'Não reage a efeitos primários' in anchor.statement


def test_extraction_schema_requires_each_owner_and_limits_its_source_ids():
    from suno_asset.services.literal import EXTRACTION_SCHEMA, _schema_for
    source = prepared('1. Selic 13,75%.\n2. Inflação desacelerou.\n3. Risco externo.\n4. Crédito moderou.')
    schema = _schema_for(EXTRACTION_SCHEMA, source, source.blocks)
    slots = schema['properties']['blocks']
    assert set(slots['required']) == {b.block_id for b in source.blocks}
    assert slots['additionalProperties'] is False
    for block in source.blocks:
        fields = slots['properties'][block.block_id]['anyOf'][0]['properties']
        anchor = fields['anchors']['items']['properties']
        allowed = set((*block.excerpt_ids, *block.context_ids))
        assert set(anchor['excerpt_ids']['items']['enum']) == allowed
        assert set(anchor['numeric_facts']['items']['properties']['excerpt_ids']['items']['enum']) == allowed
    first_anchor = slots['properties'][source.blocks[0].block_id]['anyOf'][0]['properties']['anchors']['items']['properties']
    assert source.blocks[-1].excerpt_ids[0] not in first_anchor['excerpt_ids']['items']['enum']


def test_literal_patch_version_uses_a_valid_transport_schema_name():
    import re
    s = session([])
    s.configure('literal-v1', 'quality')
    payload = s.payload('extract', 'dados', {}, {'type': 'object'})
    name = payload['response_format']['json_schema']['name']
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name)
    assert s.manifest()['prompt_version'] == 'literal-v1.13'
    from suno_asset.m7_acceptance import fingerprint
    assert fingerprint(Path('tests/fixtures/evaluation/corpus.json'))['literal_prompt_version'] == s.manifest()['prompt_version']


def test_numeric_metadata_choices_are_literal_and_unknown_terms_remain_optional():
    from suno_asset.services.literal import EXTRACTION_SCHEMA, _schema_for, _numeric_field_options
    text = ('1. As expectativas de inflação para 2026 na pesquisa Focus são de 4,9%. '
            'A projeção de inflação do Copom para o primeiro trimestre de 2028 é 3,2% '
            'no cenário de referência. O indicador experimental Z é de 7%.')
    source = prepared(text)
    schema = _schema_for(EXTRACTION_SCHEMA, source, source.blocks)
    fields = schema['properties']['blocks']['properties'][source.blocks[0].block_id]['anyOf'][0]['properties']['anchors']['items']['properties']['numeric_facts']['items']['properties']
    choices = _numeric_field_options([text])
    indicators = choices['indicator']
    assert 'expectativas de inflação' in indicators
    assert 'expectativa de inflação 2026' not in indicators
    assert all(value in text for value in indicators)
    assert fields['indicator_id']['type'] == ['string','null']
    assert None in fields['indicator_id']['enum']
    assert all(value is None or value.startswith('X') for value in fields['indicator_id']['enum'])
    assert fields['unit_id']['type'] == ['string','null'] and None in fields['unit_id']['enum']
    assert 'primeiro trimestre de 2028' in choices['period']
    assert _numeric_field_options(['Indicador experimental Z: 7.'])['indicator'] == []
    facts = raw_extraction(source)
    eid = source.blocks[0].excerpt_ids[0]
    facts['blocks'][0]['anchors'][0]['numeric_facts'] = [{'original':'7%', 'excerpt_ids':[eid],
        'indicator':None,'unit':'%','period':None,'attribution':None,'qualifiers':[]}]
    assert consolidate(source, _Extraction.model_validate_json(json.dumps(facts)), source.blocks, 'test').anchors[0].numeric_facts[0].indicator is None


def test_nonmaterial_wire_branch_cannot_carry_anchors():
    from suno_asset.services.literal import _WireExtraction, EXTRACTION_SCHEMA, _schema_for
    source = prepared('Participantes: membros do Comitê.')
    bid = source.blocks[0].block_id
    row = {'classification': 'administrative', 'reason': 'lista administrativa', 'anchors': None}
    result = _WireExtraction.model_validate_json(json.dumps({'blocks': {bid: row}})).ordered(source,source.blocks)
    assert result.blocks[0].anchors == []
    schema = _schema_for(EXTRACTION_SCHEMA, source, source.blocks)
    assert schema['properties']['blocks']['properties'][bid]['anyOf'][1]['properties']['anchors'] == {'type': 'null'}
    row['anchors'] = raw_extraction(source)['blocks'][0]['anchors']
    with pytest.raises(ValidationError):
        _WireExtraction.model_validate_json(json.dumps({'blocks': {bid: row}}))


def test_optional_neighbour_attribution_is_transportable_but_rejected_without_local_support():
    from suno_asset.services.literal import EXTRACTION_SCHEMA, _schema_for, block_payload
    source = prepared('1. O Comitê avaliou o cenário.\n\n2. A projeção de inflação para 2026 é 5,2%.')
    first, second = source.blocks
    neighbour = first.excerpt_ids[0]
    second = second.model_copy(update={'context_ids': (neighbour,), 'required_context_ids': ()})
    source = source.model_copy(update={'blocks': (first, second)})
    fields = _schema_for(EXTRACTION_SCHEMA, source, (second,))['properties']['blocks']['properties'][second.block_id]['anyOf'][0]['properties']['anchors']['items']['properties']['numeric_facts']['items']['properties']
    assert fields['attribution_id']['type'] == ['string','null'] and None in fields['attribution_id']['enum']
    assert neighbour in fields['excerpt_ids']['items']['enum']
    catalog = block_payload(source, (second,))['excerpts']
    assert next(row['text'] for row in catalog if row['id'] == neighbour) == source.excerpts[0].text
    output = raw_extraction(source)
    output['blocks'] = [output['blocks'][1]]
    output['blocks'][0]['anchors'][0]['numeric_facts'] = [{
        'original': '5,2%', 'excerpt_ids': list(second.excerpt_ids), 'indicator': 'inflação',
        'unit': '%', 'period': '2026', 'attribution': None, 'qualifiers': []}]
    result = consolidate(source, _Extraction.model_validate_json(json.dumps(output)), (second,), 'local')
    assert result.anchors[0].numeric_facts[0].original == '5,2%'
    output['blocks'][0]['anchors'][0]['numeric_facts'][0]['attribution'] = 'Comitê'
    with pytest.raises(ExpectedServiceError, match='campos não literais'):
        consolidate(source, _Extraction.model_validate_json(json.dumps(output)), (second,), 'local')
    required = second.model_copy(update={'required_context_ids': (neighbour,)})
    fields = _schema_for(EXTRACTION_SCHEMA, source, (required,))['properties']['blocks']['properties'][required.block_id]['anyOf'][0]['properties']['anchors']['items']['properties']['numeric_facts']['items']['properties']
    assert fields['attribution_id']['type'] == ['string','null'] and None in fields['attribution_id']['enum']
    result = consolidate(source, _Extraction.model_validate_json(json.dumps(output)), (required,), 'mandatory')
    assert result.anchors[0].numeric_facts[0].attribution == 'Comitê'


def test_metadata_choices_preserve_original_line_breaks():
    from suno_asset.services.literal import _numeric_field_options
    text = 'O preço do\npetróleo aumenta 2% ao ano no cenário de\nreferência.'
    choices = _numeric_field_options([text])
    assert 'preço do\npetróleo' in choices['indicator']
    assert 'preço do petróleo' not in choices['indicator']
    assert choices['qualifiers'] == ['cenário de\nreferência']


def test_missing_keyed_block_is_repaired_without_silently_losing_source():
    source = prepared()
    seen = []
    def omit_once(value, data):
        if 'errors' not in data:
            value['blocks'].pop()
    result = LiteralExtractor(session(seen, mutate_extract=omit_once)).extract_prepared(source)
    assert len(result.corrections) == 1
    assert result.corrections[0]['error']['code'] == 'invalid_block_processing'
    assert {r.block_id for r in result.processing} == {b.block_id for b in source.blocks}
    assert [stage for stage, _, _ in seen] == ['verify', 'extract', 'extract']


def test_budget_insufficient_never_sends_partial_extraction():
    s=session([]);s.max_calls=1
    with pytest.raises(ExpectedServiceError):LiteralExtractor(s).extract_prepared(prepared())
    assert s.calls==0


@pytest.mark.parametrize('profile',['quality','hybrid'])
def test_profiles_share_one_budget_and_route_corrections_to_extraction_model(profile,tmp_path):
    seen=[]
    def bad_once(value,data):
        if 'errors' not in data:value['blocks'][0]['anchors'][0]['excerpt_ids']=['UNKNOWN']
    s=session(seen,mutate_extract=bad_once);store=LocalStorage(tmp_path)
    request=GenerationRequest(request_id=uuid4(),source=store.put_text('1. O Copom decidiu manter a Selic em 13,75% ao ano.'),
        mode='real',architecture='literal-v1',model_profile=profile,audience='beginner',output_format='analytical_text')
    result=run_workflow(request,services=_real_services(store,s),policy=LITERAL_POLICY)
    assert result.status.value=='approved',result.errors
    assert result.manifest['profile']==profile
    assert len([stage for stage,_,_ in seen if stage=='extract'])==2
    for stage,model,_ in seen:assert model==('openai/gpt-oss-20b' if profile=='hybrid' and stage=='extract' else 'openai/gpt-oss-120b')
    assert result.llm_attempts==len(seen)==s.calls
    assert all(claim.anchor_ids for claim in result.content.claims)
    assert sum(v['calls'] for v in cost_records(s).values())==s.calls
    assert list((tmp_path/'runs').glob('*-prepared-source.json'))
    for ref in result.references:assert store.resolve_evidence(ref).text in result.content.development


def test_citing_an_anchor_without_preserving_it_fails_coverage():
    source=prepared();extraction=SimulatedLiteralExtractor().extract_prepared(source);_,evidence=source_artifacts(source)
    seen=[]
    def omit(value,data):
        for b in value['development']:b['text']='O Comitê discutiu questões relevantes.'
    def verdict(value,data):
        for row in value['facts']:row.update(result='failed',reason='negação/condição não preservada',claim_ids=[])
    s=session(seen,mutate_write=omit,mutate_coverage=verdict)
    request=GenerationRequest(request_id=uuid4(),source=LocalStorage('/tmp/literal-test-request').put_text('fonte'),
        audience='beginner',output_format='analytical_text')
    content=LiteralGenerator(s).generate(request,extraction.anchors,evidence)
    report=LiteralEvaluator(s).evaluate(content,extraction.anchors,evidence,LITERAL_POLICY)
    assert not report.approved and all(f.result.value=='failed' for f in report.critical_facts)


def test_economy_evaluator_groups_claim_support_and_editorial_checks_into_one_review():
    source=prepared('1. O Copom decidiu reduzir a Selic para 13,75% ao ano.')
    extraction=SimulatedLiteralExtractor().extract_prepared(source)
    _,evidence=source_artifacts(source)
    anchors=extraction.anchors
    claims=tuple(Claim(claim_id=f'claim-{i}',text=a.statement,evidence_refs=a.evidence_refs,
        anchor_ids=(a.anchor_id,)) for i,a in enumerate(anchors,1))
    content=GeneratedContent(content_id=uuid4(),revision=0,audience='beginner',output_format='analytical_text',
        title='Análise da ata do Copom',synthesis=claims[0].text,
        development='\n\n'.join(c.text for c in claims),implications=claims[0].text,claims=claims)
    seen=[]
    def review(payload,*_):
        data=json.loads(payload['messages'][1]['content']);seen.append(data)
        return response({'claims':[{'claim_id':c['claim_id'],'support':'passed','numbers':'passed',
            'citations':'passed','reason':'claim e fonte conferidos'} for c in data['claims']],
            'facts':[{'anchor_id':a['id'],'result':'passed','reason':'fato integral preservado',
                'claim_ids':a['candidate_claim_ids'],'conditions':'passed','negations':'passed',
                'modality':'passed','attribution':'passed'} for a in data['anchors']],
            'coherence':'passed','adequacy':'passed','reason':'peça coerente para o público'})
    s=GroqSession('fake',transport=review,counter=Counter(),profile='economy')
    report=LiteralEvaluator(s).evaluate(content,anchors,evidence,LITERAL_POLICY)
    assert report.approved and s.calls==1 and len(seen)==1
    assert s.literal_artifacts[-1]['stage']=='mvp_semantic_review'
    assert set(seen[0])=={'title','audience','output_format','format_metrics','claims','anchors','source_catalog'}


def test_literal_generation_passes_editorial_focus_as_data_and_instruction():
    source=prepared('1. O Copom decidiu reduzir a Selic para 13,75% ao ano.')
    extraction=SimulatedLiteralExtractor().extract_prepared(source)
    _,evidence=source_artifacts(source);seen=[]
    request=GenerationRequest(request_id=uuid4(),source=LocalStorage('/tmp/editorial-focus-test').put_text('fonte'),
        audience='beginner',output_format='analytical_text',editorial_focus='Não vincule projeções a causas que a fonte não declara.')
    def capture(payload,*_):
        data=json.loads(payload['messages'][1]['content']);system=payload['messages'][0]['content']
        seen.append((data,system))
        block={'text':anchors[0].statement,'anchor_ids':[anchors[0].anchor_id],'glossary_term':''}
        return response({'synthesis':[block],'development':[block],'implications':[block]})
    anchors=extraction.anchors
    s=GroqSession('fake',transport=capture,counter=Counter())
    LiteralGenerator(s).generate(request,extraction.anchors,evidence)
    assert len(seen)==1 and seen[0][0]['editorial_focus']==request.editorial_focus
    assert 'Trate-a como preferência de apresentação' in seen[0][1]


def test_economy_writer_receives_six_screen_and_500_word_limits():
    source=prepared('1. O Copom decidiu reduzir a Selic para 13,75% ao ano.')
    extraction=SimulatedLiteralExtractor().extract_prepared(source)
    _,evidence=source_artifacts(source);seen=[]
    def capture(payload,*_):
        data=json.loads(payload['messages'][1]['content']);seen.append(data)
        block={'text':extraction.anchors[0].statement,'anchor_ids':[extraction.anchors[0].anchor_id],'glossary_term':''}
        if data['output_format']=='informational_carousel':
            return response({'hook':[block],'cards':[block],'conclusion':[block]})
        return response({'synthesis':[block],'development':[block],'implications':[block]})
    session=GroqSession('fake',transport=capture,counter=Counter(),profile='economy')
    request=GenerationRequest(request_id=uuid4(),source=LocalStorage('/tmp/economy-format-test').put_text('fonte'),
        audience='intermediate',output_format='informational_carousel')
    LiteralGenerator(session).generate(request,extraction.anchors,evidence)
    assert 'seis telas' in seen[0]['format_instruction']
    assert seen[0]['batch_budget']['global_max_cards']==4


def test_economy_generation_escalates_only_terminal_schema_rejection_batch():
    from io import BytesIO
    from urllib.error import HTTPError
    source=prepared('1. O Copom decidiu reduzir a Selic para 13,75% ao ano.')
    extraction=SimulatedLiteralExtractor().extract_prepared(source)
    _,evidence=source_artifacts(source);models=[]
    def capture(payload,*_):
        model=payload['model'];models.append(model)
        if model=='openai/gpt-oss-20b':
            body=b'{"error":{"code":"json_validate_failed","message":"Generated JSON does not match the expected schema"}}'
            raise HTTPError('https://api.groq.com',400,'schema',{},BytesIO(body))
        block={'text':extraction.anchors[0].statement,
            'anchor_ids':[extraction.anchors[0].anchor_id],'glossary_term':''}
        return response({'synthesis':[block],'development':[block],'implications':[block]})
    s=GroqSession('fake',transport=capture,counter=Counter(),profile='economy')
    request=GenerationRequest(request_id=uuid4(),source=LocalStorage('/tmp/economy-escalation-test').put_text('fonte'),
        audience='beginner',output_format='analytical_text')
    content=LiteralGenerator(s).generate(request,extraction.anchors,evidence)
    assert content.claims and models==['openai/gpt-oss-20b','openai/gpt-oss-20b','openai/gpt-oss-120b']
    assert 'generate' not in s.model_overrides
    assert s.literal_artifacts[-1]['stage']=='generate'
    assert any(row['stage']=='economy_batch_escalation' for row in s.literal_artifacts)


def test_mixed_glossary_label_preserves_anchored_material_block():
    source=prepared('1. O Copom decidiu reduzir a Selic para 13,75% ao ano.')
    extraction=SimulatedLiteralExtractor().extract_prepared(source);_,evidence=source_artifacts(source)
    def mislabel(value,data):
        value['implications'][0]['glossary_term']='Copom'
    s=session([],mutate_write=mislabel)
    request=GenerationRequest(request_id=uuid4(),source=LocalStorage('/tmp/literal-mixed-glossary').put_text('fonte'),
        audience='beginner',output_format='analytical_text')

    content=LiteralGenerator(s).generate(request,extraction.anchors,evidence)

    assert '13,75%' in content.implications
    assert any(claim.anchor_ids and '13,75%' in claim.text for claim in content.claims)


def test_refinement_cannot_trade_preserved_critical_facts_even_when_general_checks_pass():
    checks=tuple(EvaluationCheck(name=name,result='passed') for name in LITERAL_POLICY.required_checks)
    def report(revision,states):
        return EvaluationReport(revision=revision,policy=LITERAL_POLICY,checks=checks,approved=False,
            critical_facts=tuple(CriticalFactPreservation(anchor_id=aid,result=result,reason='offline') for aid,result in states.items()))
    assert not _no_regression(report(0,{'number':'failed','condition':'passed'}),report(1,{'number':'passed','condition':'failed'}))
    assert _no_regression(report(0,{'number':'failed','condition':'passed'}),report(1,{'number':'passed','condition':'passed'}))


def test_late_evaluation_failure_preserves_text_for_review(tmp_path):
    s=session([],fail_stage='evaluate');store=LocalStorage(tmp_path)
    req=GenerationRequest(request_id=uuid4(),source=store.put_text('1. O Copom manteve a taxa em 13,75%.'),
        mode='real',architecture='literal-v1',model_profile='quality',audience='beginner',output_format='analytical_text')
    result=run_workflow(req,services=_real_services(store,s),policy=LITERAL_POLICY)
    assert result.status.value=='needs_review' and result.content and result.evaluation is None


def test_api_defaults_old_artifacts_simulated_literal_and_evidence_resolution(tmp_path):
    store=LocalStorage(tmp_path);client=TestClient(create_app(storage=store))
    source=client.post('/documents',json={'text':'1. O Copom manteve a Selic em 13,75%.\n\n2. Não antecipa cortes.'}).json()
    old=client.post('/generations',json={'source_id':source['source_id']}).json()
    assert old['architecture']=='current' and old['model_profile']=='single'
    historical={k:v for k,v in old.items() if k not in {'architecture','model_profile','manifest','prepared_source','extraction'}}
    restored=ExecutionResult.model_validate(historical)
    assert restored.extraction is None
    new=client.post('/generations',json={'source_id':source['source_id'],'architecture':'literal-v1'}).json()
    assert new['status']=='approved' and new['extraction'] and new['fact_verification']['processing_complete']
    resolved=client.get(f'/generations/{new["request_id"]}/evidence/0').json()
    assert resolved['text'] in new['content']['development']
    assert all(c['anchor_ids'] for c in new['content']['claims'])


def test_empty_material_block_cannot_hide_critical_fact(tmp_path):
    def discard(value,data):
        value['blocks'][0].update(classification='no_relevant_facts',reason='sem relevância',anchors=[])
    def coverage(value,data):
        first=value['blocks'][0]
        first.update(coverage='failed',relevance='failed',omission_excerpt_ids=data['blocks'][0]['excerpt_ids'],
            reason='condição qualitativa crítica descartada')
    store=LocalStorage(tmp_path);s=session([],mutate_extract=discard,mutate_verify=coverage)
    req=GenerationRequest(request_id=uuid4(),source=store.put_text('1. Não antecipa cortes enquanto persistirem riscos.\n2. Há riscos de inflação.'),
        audience='beginner',output_format='analytical_text',mode='real',architecture='literal-v1',model_profile='quality')
    result=run_workflow(req,services=_real_services(store,s),policy=LITERAL_POLICY)
    assert result.status.value=='failed' and result.fact_verification is None
    assert [error.code for error in result.errors]==['literal_extraction_failed']
    assert len(s.extraction_corrections)==1
    assert s.extraction_corrections[0]['error']['code']=='source_materiality_conflict'
    assert any(event['stage']=='source_materiality' for event in s.literal_artifacts)
    assert not any(attempt['stage'] in {'generate','evaluate'} for attempt in s.attempt_records)


def test_page_continuation_skips_repeated_header_and_footer_but_keeps_original_evidence():
    source=prepare_source(units('ATA DO COMITÊ\nData da reunião\n1. O Comitê não reage a choques,\n1 bcb.gov.br',
        'ATA DO COMITÊ\nData da reunião\ncaso não haja efeitos de segunda ordem.\n2. Outros riscos.\n2 bcb.gov.br'),Counter())
    continued=[b for b in source.blocks if 'cross_page_continuation' in b.diagnostics]
    assert len(continued)==1 and len(continued[0].excerpt_ids)==2
    original='\n'.join(e.text for e in source.excerpts if e.excerpt_id in continued[0].excerpt_ids)
    assert 'não reage' in original and 'caso não haja' in original
    assert 'bcb.gov.br' not in original
    assert sum(len(e.text.strip()) for e in source.excerpts)>len(original.strip())


def test_single_model_environment_configuration_is_preserved():
    s=session([]);s.model='openai/gpt-oss-20b';s.configure('current','single')
    assert all(s.model_for(stage)=='openai/gpt-oss-20b' for stage in ('extract','verify','generate','evaluate','refine'))
    s.configure('literal-v1','quality')
    assert all(s.model_for(stage)=='openai/gpt-oss-120b' for stage in ('extract','verify','generate','evaluate','refine'))


def test_economy_profile_uses_20b_only_for_extraction_and_drafting():
    s=session([]);s.configure('literal-v1','economy')
    assert {stage:s.model_for(stage) for stage in ('extract','verify','generate','evaluate','refine')} == {
        'extract':'openai/gpt-oss-20b','verify':'openai/gpt-oss-120b',
        'generate':'openai/gpt-oss-20b','evaluate':'openai/gpt-oss-120b','refine':'openai/gpt-oss-120b'}


def test_literal_real_api_idempotency_binds_architecture_and_profile(tmp_path,monkeypatch):
    from suno_asset.api import app as api
    s=session([]);monkeypatch.setattr(api,'make_session',lambda:s)
    client=TestClient(create_app(storage=LocalStorage(tmp_path)))
    source=client.post('/documents',json={'text':'1. O Copom mantém a Selic em 13,75%.'}).json()
    payload={'source_id':source['source_id'],'mode':'real','architecture':'literal-v1','model_profile':'hybrid'}
    first=client.post('/generations',json=payload,headers={'Idempotency-Key':'literal-test-key'})
    calls=s.calls
    repeated=client.post('/generations',json=payload,headers={'Idempotency-Key':'literal-test-key'})
    assert first.status_code==200 and repeated.json()==first.json() and calls==s.calls
    conflict=client.post('/generations',json={**payload,'model_profile':'quality'},headers={'Idempotency-Key':'literal-test-key'})
    assert conflict.status_code==409


def test_cost_is_based_on_the_model_of_each_call():
    s=session([])
    s.records=[{'stage':'extract','model':'openai/gpt-oss-20b','prompt_tokens':1000000,'completion_tokens':1000000},
        {'stage':'verify','model':'openai/gpt-oss-120b','prompt_tokens':1000000,'completion_tokens':1000000}]
    cost=cost_records(s)
    assert cost['extract']['estimated_usd']==pytest.approx(.375)
    assert cost['verify']['estimated_usd']==pytest.approx(.75)


@pytest.mark.parametrize('interrupted', [False, True])
def test_call_audit_survives_interruption_without_recording_credentials(tmp_path, interrupted):
    from suno_asset.m7_literal_eval import audited_transport
    from suno_asset.services.groq import EXTRACT_SCHEMA
    from suno_asset.services.llm_budget import RateGate

    def send(payload, key, timeout):
        # The request must already be durable when the provider is invoked.
        started = tmp_path / 'call-audit/attempt-001-started.json'
        assert started.is_file() and key == 'private-test-key'
        if interrupted:
            raise KeyboardInterrupt()
        return response({'anchors': []})

    gate = RateGate(enforce_window=False)
    s = GroqSession('private-test-key', transport=send, rate_gate=gate, counter=Counter())
    s.transport = audited_transport(s, tmp_path)
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            s.call('extract', 'fonte é dado', {}, EXTRACT_SCHEMA)
    else:
        s.call('extract', 'fonte é dado', {}, EXTRACT_SCHEMA)
    assert s.rate_gate is gate
    artifacts = list((tmp_path / 'call-audit').glob('*.json'))
    assert len(artifacts) == 2
    assert all('private-test-key' not in p.read_text() for p in artifacts)
    if interrupted:
        checkpoint = json.loads((tmp_path / 'call-audit/attempt-001-interrupted.json').read_text())
        assert checkpoint['usage_known'] is False
    else:
        checkpoint = json.loads((tmp_path / 'call-audit/attempt-001-received.json').read_text())
        assert checkpoint['response']['usage']['completion_tokens'] == 10


def test_http_audit_keeps_provider_body_readable_for_session_error_policy(tmp_path):
    from io import BytesIO
    from urllib.error import HTTPError
    from suno_asset.m7_literal_eval import audited_transport
    from suno_asset.services.groq import EXTRACT_SCHEMA

    def send(*_):
        body = json.dumps({'error': {'code': 'json_validate_failed',
            'message': 'schema problem private-test-key', 'failed_generation': '{"unit":"%"}'}}).encode()
        raise HTTPError('https://api.groq.com', 400, 'Bad Request', {}, BytesIO(body))

    s = GroqSession('private-test-key', transport=send, counter=Counter(), max_calls=1)
    s.transport = audited_transport(s, tmp_path)
    with pytest.raises(ExpectedServiceError):
        s.call('extract', 'fonte é dado', {}, EXTRACT_SCHEMA)
    assert s.attempt_records[0]['provider_code'] == 'json_validate_failed'
    p = tmp_path / 'call-audit/attempt-001-interrupted.json'
    diagnostic = json.loads(p.read_text())
    assert 'private-test-key' not in p.read_text()
    assert '[REDACTED]' in diagnostic['provider_response']
    assert json.loads(diagnostic['provider_response'])['error']['failed_generation'] == '{"unit":"%"}'


def test_generated_title_and_unlinked_fact_are_rejected():
    source=prepared();extraction=SimulatedLiteralExtractor().extract_prepared(source);_,evidence=source_artifacts(source)
    from suno_asset.services.literal import SimulatedLiteralGenerator
    req=GenerationRequest(request_id=uuid4(),source=LocalStorage('/tmp/literal-title-test').put_text('fonte'),
        audience='beginner',output_format='analytical_text')
    content=SimulatedLiteralGenerator().generate(req,extraction.anchors,evidence).model_copy(update={
        'title':'Selic cairá para 5%','implications':'A Selic cairá para 5% em 2030.'})
    report=LiteralEvaluator(session([])).evaluate(content,extraction.anchors,evidence,LITERAL_POLICY)
    assert not report.approved and next(c for c in report.checks if c.name=='structure').result.value=='failed'


def test_indivisible_support_is_not_summarized_or_cut_for_writing():
    source=prepared('1. Fato crítico.');extraction=SimulatedLiteralExtractor().extract_prepared(source)
    s=session([]);s.tpm_limit=1000
    with pytest.raises(ExpectedServiceError):LiteralGenerator(s)._write('beginner',0,extraction.anchors,(),None,None)
    assert s.calls==0


def test_full_previous_text_is_partitioned_into_exact_claims_for_refinement(tmp_path):
    from suno_asset.services.literal import SimulatedLiteralGenerator
    source=prepared('\n'.join(f'{i}. O Comitê deve manter cautela e considerar '+('as condições econômicas ' * 15)+'.'
        for i in range(1,61)))
    extraction=SimulatedLiteralExtractor().extract_prepared(source)
    _,evidence=source_artifacts(source)
    request=GenerationRequest(request_id=uuid4(),source=LocalStorage(tmp_path).put_text('fonte'),
        audience='beginner',output_format='analytical_text')
    previous=SimulatedLiteralGenerator().generate(request,extraction.anchors,evidence)
    seen=[];s=session(seen)
    after=LiteralGenerator(s)._write('beginner',1,extraction.anchors,evidence,previous,())
    assert after.claims
    payloads=[data for stage,_,data in seen if stage=='refine']
    assert len(payloads)>1
    original={c.claim_id:c for c in previous.claims}
    received=set()
    for data in payloads:
        ids={a['id'] for a in data['anchors']}
        assert data['previous']['scope']=='claims_for_batch'
        for c in data['previous']['claims']:
            assert c['text']==original[c['claim_id']].text
            assert ids.intersection(c['anchor_ids'])
            received.add(c['claim_id'])
    assert received==set(original)


def test_portuguese_ordinal_with_dot_is_not_an_invented_quantity():
    from suno_asset.services.groq import _numbers
    assert _numbers('1.º trimestre de 2028, inflação de 1,25%, e taxa de 2.5%.')=={'2028','1,25%','2,5%'}


def test_full_ata_numeric_injection_preserves_other_critical_fact_and_refinement_restores_both():
    from suno_asset.m7_literal_refinement import inject_numeric_error
    from suno_asset.services.groq import GroqRefiner
    source=prepared('1. Selic 13,75% ao ano.\n\n2. Não antecipa cortes enquanto persistirem riscos.')
    raw=raw_extraction(source)
    first=raw['blocks'][0]['anchors'][0];first['kind']='decision'
    first['numeric_facts']=[{'original':'13,75%','excerpt_ids':first['excerpt_ids'],'indicator':'Selic','unit':'%',
        'period':None,'attribution':None,'qualifiers':[]}]
    extraction=consolidate(source,_Extraction.model_validate_json(json.dumps(raw)),source.blocks,'test')
    _,evidence=source_artifacts(source)
    def verdict(value,data):
        for row in value['facts']:
            if not row['claim_ids']:row.update(result='failed',reason='valor crítico alterado')
    s=session([],mutate_coverage=verdict)
    req=GenerationRequest(request_id=uuid4(),source=LocalStorage('/tmp/literal-refinement-unit').put_text('fonte'),
        audience='beginner',output_format='analytical_text')
    generator=LiteralGenerator(s);original=generator.generate(req,extraction.anchors,evidence)
    before,mutation=inject_numeric_error(original,extraction.anchors)
    assert mutation['original']=='13,75%' and mutation['injected']=='14,75%'
    assert 'Não antecipa cortes enquanto persistirem riscos.' in before.development
    evaluator=LiteralEvaluator(s);first_report=evaluator.evaluate(before,extraction.anchors,evidence,LITERAL_POLICY)
    assert not first_report.approved
    after=GroqRefiner(generator).refine(before,first_report.issues,extraction.anchors,evidence,1)
    second_report=evaluator.evaluate(after,extraction.anchors,evidence,LITERAL_POLICY)
    assert second_report.approved and _no_regression(first_report,second_report)
    assert all(f.result.value=='passed' for f in second_report.critical_facts)


def test_reserved_ata_never_reaches_provider_in_new_comparison(tmp_path,monkeypatch):
    from suno_asset import m7_literal_eval
    import sys
    monkeypatch.setattr(sys,'argv',['comparison','--source-id','copom279-20260617','--repeats','1',
        '--max-cost-usd','.9','--out',str(tmp_path/'reserve')])
    monkeypatch.setattr(m7_literal_eval,'make_session',lambda:pytest.fail('provider must not be called'))
    with pytest.raises(SystemExit):m7_literal_eval.main()
    assert not (tmp_path/'reserve').exists()


def test_historical_v2_freeze_rejects_incomplete_comparison(tmp_path,monkeypatch):
    from suno_asset import m7_literal_acceptance as acceptance
    monkeypatch.setattr(acceptance,'validate_review',lambda *_:{'glossary_accepted':True})
    monkeypatch.setattr(acceptance,'validate_account',lambda *_:None)
    current_fingerprint=acceptance.fingerprint(Path('tests/fixtures/evaluation/corpus.json'))
    files=[tmp_path/name for name in ('form.json','account.json','outputs.json','cases.json')]
    for path in files:path.write_text('{}')
    monkeypatch.setattr(acceptance,'validate_outputs',lambda *_:{'run_fingerprint':current_fingerprint,'run_rows':[]})
    with pytest.raises(ValueError,match='18 no total'):
        acceptance.freeze_configuration_v2('literal-quality',files[0],files[1],files[2],Path('tests/fixtures/evaluation/corpus.json'),files[3])
    monkeypatch.setattr(acceptance,'validate_outputs',lambda *_:{'run_fingerprint':{'old_model_only':True}})
    with pytest.raises(ValueError,match='outra arquitetura'):
        acceptance.freeze_configuration_v2('literal-quality',files[0],files[1],files[2],Path('tests/fixtures/evaluation/corpus.json'),files[3])


def test_model_approval_cannot_override_critical_fact_failure():
    checks=tuple(EvaluationCheck(name=name,result='passed') for name in LITERAL_POLICY.required_checks)
    with pytest.raises(ValidationError,match='preservação individual'):
        EvaluationReport(revision=0,policy=LITERAL_POLICY,checks=checks,approved=True,
            critical_facts=(CriticalFactPreservation(anchor_id='lost-fact',result='failed',reason='fato omitido'),))


def test_numeric_metadata_whitespace_uses_exact_cited_source_and_audits_raw_model():
    source = prepared('1. O preço do\npetróleo aumenta 2% no cenário de\nreferência em 2026.')
    raw = raw_extraction(source)
    anchor = raw['blocks'][0]['anchors'][0]
    anchor['numeric_facts'] = [{'original': '2%', 'excerpt_ids': anchor['excerpt_ids'],
        'indicator': 'preço do petróleo', 'unit': '%', 'period': '2026',
        'attribution': None, 'qualifiers': ['cenário de referência']}]
    unchanged = deepcopy(raw)
    adjustments = []
    result = consolidate(source, _Extraction.model_validate_json(json.dumps(raw)), source.blocks, 'test',
        metadata_adjustments=adjustments)
    fact = result.anchors[0].numeric_facts[0]
    assert fact.indicator == 'preço do\npetróleo' and fact.qualifiers == ('cenário de\nreferência',)
    assert fact.original == '2%' and fact.period == '2026'
    assert fact.excerpt_ids == tuple(anchor['excerpt_ids'])
    assert raw == unchanged and result.anchors[0].statement == source.excerpts[0].text
    assert {a['field'] for a in adjustments} == {'indicator', 'qualifiers'}
    assert all(a['scope'] == 'whitespace_only' for a in adjustments)
    assert adjustments[0]['before'] == 'preço do petróleo'
    assert adjustments[0]['after'] == 'preço do\npetróleo'


@pytest.mark.parametrize('change', [{'indicator':'preço do Petróleo'}, {'indicator':'preço do gás'},
    {'period':'2027'}, {'unit':'p.p.'}, {'attribution':'Fed'}, {'original':'3%'},
    {'original':'amarela'}, {'qualifiers':['abaixo da meta']}])
def test_metadata_canonicalization_does_not_repair_content_or_wrong_authors(change):
    source = prepared('1. O preço do\npetróleo aumenta 2% em 2026 no cenário de\nreferência.')
    raw = raw_extraction(source)
    anchor = raw['blocks'][0]['anchors'][0]
    fact = {'original': '2%', 'excerpt_ids': anchor['excerpt_ids'], 'indicator': 'preço do petróleo',
        'unit': '%', 'period': '2026', 'attribution': None, 'qualifiers': ['cenário de referência']}
    fact.update(change); anchor['numeric_facts'] = [fact]
    with pytest.raises(ExpectedServiceError):
        consolidate(source, _Extraction.model_validate_json(json.dumps(raw)), source.blocks, 'test')


def test_numeric_metadata_selectors_and_original_strings_are_both_preserved():
    source = prepared('1. O preço do\npetróleo aumenta 2% em 2026.')
    seen = []
    def numeric(value, data):
        anchor = value['blocks'][0]['anchors'][0]
        anchor['numeric_facts'] = [{'original': '2%', 'excerpt_ids': anchor['excerpt_ids'],
            'indicator': 'preço do petróleo', 'unit': '%', 'period': '2026',
            'attribution': None, 'qualifiers': []}]
    s = session(seen, mutate_extract=numeric)
    result = LiteralExtractor(s).extract_prepared(source)
    candidate = next(event for event in s.literal_artifacts if event['stage'] == 'extraction_candidate')
    wire = next(event for event in s.literal_artifacts if event['stage'] == 'extraction_wire_candidate')
    fact = wire['output']['blocks'][source.blocks[0].block_id]['anchors'][0]['numeric_facts'][0]
    assert fact['indicator_id'].startswith(source.excerpts[0].excerpt_id+'-I') and 'indicator' not in fact
    assert candidate['output']['blocks'][0]['anchors'][0]['numeric_facts'][0]['indicator'] == 'preço do\npetróleo'
    assert not any(event['stage']=='numeric_metadata_whitespace' for event in s.literal_artifacts)
    assert result.anchors[0].numeric_facts[0].indicator == 'preço do\npetróleo'
    validate_extraction(source, result)
    assert not result.corrections and s.calls == 2


def test_literal_wrong_author_is_not_sanitized_and_semantic_verification_blocks():
    source = prepared('1. O Copom projeta inflação de 5,2% em 2026. A pesquisa Focus indica 4,9% em 2026.')
    raw = raw_extraction(source)
    anchor = raw['blocks'][0]['anchors'][0]
    anchor['numeric_facts'] = [{'original': '5,2%', 'excerpt_ids': anchor['excerpt_ids'],
        'indicator': 'inflação', 'unit': '%', 'period': '2026', 'attribution': 'Focus', 'qualifiers': []}]
    extraction = consolidate(source, _Extraction.model_validate_json(json.dumps(raw)), source.blocks, 'test')
    assert extraction.anchors[0].numeric_facts[0].attribution == 'Focus'
    seen = []
    def wrong_author(value, data):
        row = value['blocks'][0]
        row.update(support='failed', reason='5,2% pertence ao Copom, não à pesquisa Focus')
        row['anchors'][0].update(result='failed', reason='atribuição incorreta do valor')
    report = LiteralFactVerifier(session(seen, mutate_verify=wrong_author)).verify_prepared(source, extraction, LITERAL_POLICY)
    assert not report.approved
    payload = next(data for stage, _, data in seen if stage == 'verify' and 'processing' in data)
    assert payload['anchors'][0]['numeric_facts'][0]['attribution'] == 'Focus'
    assert 'statement' not in payload['anchors'][0]
    assert quoted_anchor(payload,payload['anchors'][0]) == source.excerpts[0].text


def test_source_first_material_goal_blocks_candidate_false_approval_with_other_critical_anchor(tmp_path):
    text=('1. Participantes: lista administrativa. O regime de política monetária visa à meta de inflação.\n\n'
          '2. O Comitê decidiu Selic de 13,75% ao ano.')
    seen=[]
    def discard_mixed(value,data):
        value['blocks'][0].update(classification='administrative',anchors=[])
    session_obj=session(seen,mutate_extract=discard_mixed)
    store=LocalStorage(tmp_path);registered=store.put_text(text)
    request=GenerationRequest(request_id=uuid4(),source=registered,mode='real',architecture='literal-v1',
        model_profile='quality',audience='beginner',output_format='analytical_text')
    result=run_workflow(request,services=_real_services(store,session_obj),policy=LITERAL_POLICY)
    assert result.status.value=='failed' and result.fact_verification is None and not result.content
    assert [error.code for error in result.errors]==['literal_extraction_failed']
    source_event=next(event for event in session_obj.literal_artifacts if event['stage']=='source_materiality')
    first=next(row for row in source_event['output']['blocks'] if row['block_id']==result.prepared_source.blocks[0].block_id)
    assert first['classification']=='material' and first['material_excerpt_ids']
    correction=session_obj.extraction_corrections[0]
    assert correction['error']['code']=='source_materiality_conflict'
    assert correction['preserved_block_ids']==[result.prepared_source.blocks[1].block_id]
    source_inputs=[data for stage,_,data in seen if data.get('control')=='source_materiality']
    assert source_inputs and all('processing' not in data and 'anchors' not in data for data in source_inputs)
    assert not any(stage in {'generate','refine','evaluate'} for stage,_,_ in seen)


def test_uncertain_source_reading_cannot_be_overridden_by_candidate_passed():
    source=prepared()
    extraction=SimulatedLiteralExtractor().extract_prepared(source)
    def uncertain(value,data):
        value['blocks'][0].update(classification='not_verified',material_excerpt_ids=[],reason='source reading uncertain')
    report=LiteralFactVerifier(session([],mutate_source=uncertain)).verify_prepared(source,extraction,LITERAL_POLICY)
    assert report.source_materiality_complete is True and not report.approved
    assert report.block_verifications[0].coverage.value=='not_verified'
    assert report.block_verifications[0].relevance.value=='not_verified'
    assert any(check.name=='source_materiality' and check.result.value=='not_verified' for check in report.checks)


@pytest.mark.parametrize('corruption',['missing','duplicate','foreign_excerpt'])
def test_source_first_missing_or_cross_owner_control_never_reaches_candidate_comparison(corruption):
    source=prepared();extraction=SimulatedLiteralExtractor().extract_prepared(source);seen=[]
    def corrupt(value,data):
        if corruption=='missing':value['blocks'].pop()
        elif corruption=='duplicate':value['blocks'].append(deepcopy(value['blocks'][0]))
        else:value['blocks'][0]['material_excerpt_ids']=value['blocks'][1]['material_excerpt_ids']
    with pytest.raises(ExpectedServiceError) as exc:
        LiteralFactVerifier(session(seen,mutate_source=corrupt)).verify_prepared(source,extraction,LITERAL_POLICY)
    assert exc.value.code=='invalid_source_materiality'
    assert seen and all(data.get('control')=='source_materiality' for stage,_,data in seen)
