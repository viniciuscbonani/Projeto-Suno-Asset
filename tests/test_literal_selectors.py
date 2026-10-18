"""Literal selectors resolve originals; semantic association stays independent."""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from suno_asset.domain.models import ParsedUnit, ExtractionResult
from suno_asset.services.literal import (_WireExtraction, resolve_wire_extraction, extraction_payload,
    consolidate, _schema_for, EXTRACTION_SCHEMA, EXTRACT_PROMPT, _numeric_metadata_catalog, LiteralExtractor,
    SimulatedLiteralExtractor, LiteralFactVerifier)
from suno_asset.services.groq import GroqSession
from suno_asset.services.source_preparation import prepare_source
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.workflow.graph import LITERAL_POLICY


class Counter:
    mode='synthetic-selector-test'
    def count(self,text):return len(text.encode())//3+1
    def estimate(self,payload):return self.count(json.dumps(payload,ensure_ascii=False))+128


def source(text):
    return prepare_source((ParsedUnit(document_id=uuid4(),unit_id='p1',page=1,parser_version='test',
        start=0,end=len(text),text=text),),Counter())


def selected(payload,field,text,*,excerpt_id=None):
    prefix={'indicator':'I','unit':'U','period':'P','attribution':'A','qualifiers':'Q'}[field]
    return next(selector for selector,value in payload['numeric_metadata_catalog'].items()
        if selector.rsplit('-',1)[-1].startswith(prefix) and value==text
        and (excerpt_id is None or selector.startswith(excerpt_id+'-')))


def fact(payload,eid,original,**values):
    result={'original':original,'excerpt_ids':[eid],
        **{field+'_id':selected(payload,field,value,excerpt_id=eid) if value is not None else None
           for field,value in {**dict.fromkeys(('indicator','unit','period','attribution')),**values}.items()},
        'qualifier_ids':[]}
    return result


def parsed_wire(value):
    return _WireExtraction.model_validate_json(json.dumps(value))


def row(eid,facts):
    return {'classification':'material','reason':'source-selected synthetic fixture',
        'anchors':[{'kind':'number','importance':'critical','excerpt_ids':[eid],'numeric_facts':facts}]}


def blind_response(payload):
    output={'blocks':[{'block_id':block['block_id'],
        'classification':'no_relevant_facts' if block['kind']=='section' else 'material',
        'material_excerpt_ids':[] if block['kind']=='section' else block['excerpt_ids'],
        'reason':'Synthetic source-only fixture.'} for block in payload['blocks']]}
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}],
        'usage':{'prompt_tokens':10,'completion_tokens':10}}


def test_keyed_input_preserves_source_quotes_ids_offsets_and_all_block_context():
    prepared=source('1. Selic 13,75% em 2026.\n\n2. IPCA 4,9% em 2027.')
    payload=extraction_payload(prepared,prepared.blocks)
    assert list(payload['blocks'])==[block.block_id for block in prepared.blocks]
    assert all('block_id' not in value for value in payload['blocks'].values())
    assert len(payload['excerpts'])==len({row['id'] for row in payload['excerpts']})
    by_id={excerpt.excerpt_id:excerpt for excerpt in prepared.excerpts}
    for excerpt in payload['excerpts']:
        assert excerpt['text']==by_id[excerpt['id']].text
        assert excerpt['location']==by_id[excerpt['id']].ref.model_dump(mode='json',include={'page','start','end'})
    for selector in _numeric_metadata_catalog(prepared,prepared.blocks):
        assert selector['text']==by_id[selector['excerpt_id']].text[selector['start']:selector['end']]
        assert payload['numeric_metadata_catalog'][selector['id']]==selector['text']
    with pytest.raises(ValidationError):parsed_wire({'blocks':[]})


def test_context_owners_remain_explicit_when_their_blocks_are_outside_repair_scope():
    prepared=source('1. A inflação está em 4,9%.\n\nB) Cenários e análise de riscos\n\n'
        '2. A incerteza externa continua elevada.')
    title=prepared.blocks[1]
    payload=extraction_payload(prepared,(title,))
    owners={eid:block.block_id for block in prepared.blocks for eid in block.excerpt_ids}
    originals={excerpt.excerpt_id:excerpt for excerpt in prepared.excerpts}
    assert list(payload['blocks'])==[title.block_id]
    assert len(payload['excerpts'])==3
    for excerpt in payload['excerpts']:
        assert excerpt['owner_block_id']==owners[excerpt['id']]
        assert excerpt['text']==originals[excerpt['id']].text
    assert {excerpt['owner_block_id'] for excerpt in payload['excerpts']}==set(owners.values())


def test_context_only_title_repair_names_owner_and_preserves_other_rows():
    prepared=source('1. A atividade desacelerou.\n\nB) Cenários e análise de riscos\n\n'
        '2. A incerteza externa continua elevada.')
    title=prepared.blocks[1];foreign=prepared.blocks[0].excerpt_ids[0];seen=[]
    def transport(request,*args):
        payload=json.loads(request['messages'][1]['content'])
        if payload.get('control')=='source_materiality':return blind_response(payload)
        seen.append(payload)
        output={}
        for bid,block in payload['blocks'].items():
            if bid==title.block_id:
                if len(seen)==1:output[bid]=row(foreign,[])
                else:
                    error=payload['errors'][0]
                    assert error['code']=='invalid_excerpt_reference'
                    assert title.block_id in error['message'] and 'anchors[0]' in error['message']
                    assert f'principais={list(title.excerpt_ids)}' in error['message']
                    assert foreign in error['message']
                    assert {entry['id']:entry['owner_block_id'] for entry in payload['excerpts']}[foreign]!=bid
                    output[bid]={'classification':'no_relevant_facts','reason':'Somente título; fatos pertencem a outros blocos.','anchors':None}
            else:output[bid]=row(block['excerpt_ids'][0],[])
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'blocks':output})}}],
            'usage':{'prompt_tokens':10,'completion_tokens':10}}
    session=GroqSession('synthetic-selector-key',transport=transport,counter=Counter())
    result=LiteralExtractor(session).extract_prepared(prepared)
    assert session.calls==3
    assert list(seen[1]['blocks'])==[title.block_id]
    assert result.processing[1].classification=='no_relevant_facts'
    assert not result.processing[1].anchor_ids
    assert [anchor.owner_block_id for anchor in result.anchors]==[prepared.blocks[0].block_id,prepared.blocks[2].block_id]
    assert result.corrections[0]['preserved_block_ids']==[prepared.blocks[0].block_id,prepared.blocks[2].block_id]


def test_joint_periods_use_one_exact_indicator_without_synthesized_labels():
    text=('3. As expectativas de inflação para 2026 e 2027 apuradas pela pesquisa Focus\n'
          'permanecem acima da meta, situando-se em 4,9% e 4,3%, respectivamente.\n'
          'A projeção de inflação do Copom para o primeiro trimestre de 2028 é 3,2% '
          'no cenário de\nreferência.')
    prepared=source(text);payload=extraction_payload(prepared,prepared.blocks)
    block=prepared.blocks[0];eid=block.excerpt_ids[0]
    facts=[fact(payload,eid,'4,9%',indicator='expectativas de inflação',unit='%',period='2026',attribution='Focus'),
        fact(payload,eid,'4,3%',indicator='expectativas de inflação',unit='%',period='2027',attribution='Focus'),
        fact(payload,eid,'3,2%',indicator='projeção de inflação',unit='%',period='primeiro trimestre de 2028',attribution='Copom')]
    facts[2]['qualifier_ids']=[selected(payload,'qualifiers','cenário de\nreferência')]
    wire=parsed_wire({'blocks':{block.block_id:row(eid,facts)}})
    original=deepcopy(wire.model_dump())
    domain=resolve_wire_extraction(prepared,wire,prepared.blocks)
    result=consolidate(prepared,domain,prepared.blocks,'test')
    values=result.anchors[0].numeric_facts
    assert [value.original for value in values]==['4,9%','4,3%','3,2%']
    assert [value.period for value in values]==['2026','2027','primeiro trimestre de 2028']
    assert values[1].indicator=='expectativas de inflação'
    assert values[2].qualifiers==('cenário de\nreferência',)
    assert wire.model_dump()==original and result.anchors[0].statement==text
    assert ExtractionResult.model_validate_json(result.model_dump_json())==result


@pytest.mark.parametrize('corruption',['unknown','wrong_field','foreign_excerpt','duplicate_qualifier'])
def test_invalid_selector_is_rejected_and_never_replaced_with_null(corruption):
    prepared=source('1. O Copom usa Selic 13,75% em 2026 no cenário de referência.\n\n'
        '2. A pesquisa Focus indica IPCA 4,9% em 2027.')
    first,second=prepared.blocks
    second=second.model_copy(update={'context_ids':tuple(first.excerpt_ids),'required_context_ids':()})
    prepared=prepared.model_copy(update={'blocks':(first,second)})
    payload=extraction_payload(prepared,(second,));eid=second.excerpt_ids[0]
    numeric=fact(payload,eid,'4,9%',indicator='IPCA',unit='%',period='2027',attribution='Focus')
    if corruption=='unknown':numeric['indicator_id']='UNKNOWN_SELECTOR'
    elif corruption=='wrong_field':numeric['indicator_id']=numeric['period_id']
    elif corruption=='foreign_excerpt':numeric['attribution_id']=selected(payload,'attribution','Copom')
    else:
        numeric['qualifier_ids']=[selected(payload,'qualifiers','cenário de referência')]*2
    wire=parsed_wire({'blocks':{second.block_id:row(eid,[numeric])}})
    before=deepcopy(wire.model_dump())
    with pytest.raises(ExpectedServiceError) as exc:resolve_wire_extraction(prepared,wire,(second,))
    assert exc.value.code=='unsupported_numeric_field' and wire.model_dump()==before


def test_resolver_restores_source_order_even_when_provider_keys_are_reversed():
    prepared=source('1. Selic 5,2% em 2026.\n\n2. IPCA 5,2% em 2027.')
    payload=extraction_payload(prepared,prepared.blocks)
    entries={block.block_id:row(block.excerpt_ids[0],[fact(payload,block.excerpt_ids[0],'5,2%',
        indicator='Selic' if index==0 else 'IPCA',unit='%',period='2026' if index==0 else '2027')])
        for index,block in enumerate(prepared.blocks)}
    wire=parsed_wire({'blocks':dict(reversed(list(entries.items())))})
    domain=wire.ordered(prepared,prepared.blocks)
    assert [block.block_id for block in domain.blocks]==[block.block_id for block in prepared.blocks]
    result=consolidate(prepared,domain,prepared.blocks,'test')
    assert [anchor.numeric_facts[0].period for anchor in result.anchors]==['2026','2027']


def test_historical_domain_strings_remain_available_and_wire_only_allows_selectors():
    historical=json.loads(Path('tests/fixtures/literal-v1.8-extract-wire.json').read_text())
    assert historical['version']=='literal-v1.8' and 'indicator' in historical['base_extraction_schema']['properties']['blocks']['items']['properties']['anchors']['items']['properties']['numeric_facts']['items']['properties']
    prepared=source('1. Selic 13,75% em 2026.')
    schema=_schema_for(EXTRACTION_SCHEMA,prepared,prepared.blocks)
    fields=schema['properties']['blocks']['properties'][prepared.blocks[0].block_id]['anyOf'][0]['properties']['anchors']['items']['properties']['numeric_facts']['items']['properties']
    assert set(fields)=={'original','excerpt_ids','indicator_id','unit_id','period_id','attribution_id','qualifier_ids'}
    assert 'numeric_metadata_catalog' in EXTRACT_PROMPT and 'bloco misto' in EXTRACT_PROMPT


def test_literal_selector_presence_does_not_certify_semantic_association():
    prepared=source('1. O Copom projeta inflação de 5,2% em 2026. A pesquisa Focus indica 4,9% em 2026.')
    payload=extraction_payload(prepared,prepared.blocks);block=prepared.blocks[0];eid=block.excerpt_ids[0]
    wrong=fact(payload,eid,'5,2%',indicator='inflação',unit='%',period='2026',attribution='Focus')
    domain=resolve_wire_extraction(prepared,parsed_wire({'blocks':{block.block_id:row(eid,[wrong])}}),prepared.blocks)
    result=consolidate(prepared,domain,prepared.blocks,'test')
    # Structural/literal admissibility retains the error for independent
    # semantic verification; it never reassigns the value to a different author.
    assert result.anchors[0].numeric_facts[0].attribution=='Focus'
    assert result.anchors[0].statement==prepared.excerpts[0].text


@pytest.mark.parametrize('error',['unknown_selector','unknown_block'])
def test_bounded_wire_repair_keeps_valid_owner_and_never_sends_empty_repair(error):
    prepared=source('1. Selic 13,75% em 2026.\n\n2. IPCA 4,9% em 2027.')
    seen=[]
    def transport(request,*args):
        payload=json.loads(request['messages'][1]['content'])
        if payload.get('control')=='source_materiality':return blind_response(payload)
        seen.append(payload)
        output={}
        for bid,block in payload['blocks'].items():
            eid=block['excerpt_ids'][0]
            first=bid==prepared.blocks[0].block_id
            numeric=fact(payload,eid,'13,75%' if first else '4,9%',indicator='Selic' if first else 'IPCA',
                unit='%',period='2026' if first else '2027')
            if len(seen)==1 and first and error=='unknown_selector':numeric['indicator_id']='UNKNOWN_SELECTOR'
            output[bid]=row(eid,[numeric])
        if len(seen)==1 and error=='unknown_block':output['UNOWNED']=deepcopy(next(iter(output.values())))
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'blocks':output})}}],
            'usage':{'prompt_tokens':10,'completion_tokens':10}}
    session=GroqSession('synthetic-selector-key',transport=transport,counter=Counter())
    result=LiteralExtractor(session).extract_prepared(prepared)
    assert session.calls==3 and len(result.corrections)==1
    assert list(seen[1]['blocks'])==([prepared.blocks[0].block_id] if error=='unknown_selector'
        else [block.block_id for block in prepared.blocks])
    assert [anchor.numeric_facts[0].original for anchor in result.anchors]==['13,75%','4,9%']
    assert result.anchors[0].statement==prepared.excerpts[0].text


def test_verify_catalog_reconstructs_cross_page_condition_negation_and_context_once():
    texts=('1. O Comitê não reagirá aos efeitos primários,',
        'caso não se materializem efeitos de segunda ordem.\n2. Projeções condicionais.\n'
        'Tabela 1\nVariação (%)\nÍndice de preços 2026 2027\nIPCA 5,2 3,9\nNota: Selic constante.')
    doc=uuid4()
    prepared=prepare_source(tuple(ParsedUnit(document_id=doc,unit_id=f'p{page}',page=page,parser_version='test',
        start=0,end=len(text),text=text) for page,text in enumerate(texts,1)),Counter())
    extraction=SimulatedLiteralExtractor().extract_prepared(prepared)
    anchors={anchor.anchor_id:anchor for anchor in extraction.anchors};seen=[]
    def transport(request,*args):
        payload=json.loads(request['messages'][1]['content']);seen.append(payload)
        if payload.get('control')=='source_materiality':
            output={'blocks':[{'block_id':block['block_id'],
                'classification':'material','material_excerpt_ids':block['excerpt_ids'],
                'reason':'source-only synthetic protocol check'} for block in payload['blocks']]}
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}],
                'usage':{'prompt_tokens':10,'completion_tokens':10}}
        catalogue={entry['id']:entry['text'] for entry in payload['excerpts']}
        assert len(catalogue)==len(payload['excerpts'])
        for anchor in payload['anchors']:
            assert 'statement' not in anchor
            assert '\n'.join(catalogue[eid] for eid in anchor['excerpt_ids'])==anchors[anchor['id']].statement
        output={'blocks':[{'block_id':row['block_id'],'support':'passed','coverage':'passed','relevance':'passed',
            'reason':'synthetic protocol check','anchors':[{'anchor_id':aid,'result':'passed','reason':'synthetic protocol check'}
                for aid in row['anchor_ids']],'omission_excerpt_ids':[]} for row in payload['processing']]}
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}],
            'usage':{'prompt_tokens':10,'completion_tokens':10}}
    session=GroqSession('synthetic-verification-key',transport=transport,counter=Counter())
    LiteralFactVerifier(session).verify_prepared(prepared,extraction,LITERAL_POLICY)
    all_originals='\n'.join(entry['text'] for payload in seen for entry in payload['excerpts'])
    assert 'não reagirá aos efeitos primários' in all_originals
    assert 'caso não se materializem efeitos de segunda ordem' in all_originals
    assert 'Variação (%)' in all_originals and 'Nota: Selic constante.' in all_originals
