"""Isolated proposed source-first ordering; fake transports, no provider calls."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from suno_asset.services.groq import GroqSession, LITERAL_PROMPT_VERSION
from suno_asset.services.literal import (LiteralExtractor, LiteralFactVerifier, SimulatedLiteralExtractor,
    ensure_source_materiality, _schema_for, EXTRACTION_SCHEMA, _WireExtraction, resolve_wire_extraction)
from suno_asset.services.source_preparation import source_artifacts
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.workflow.graph import LITERAL_POLICY
from test_literal_selectors import Counter, source, row


def source_responder(seen, *, bad_first=False):
    def transport(payload,*_):
        data=json.loads(payload['messages'][1]['content']);seen.append((payload['model'],data))
        if data.get('control')=='source_materiality':
            assert 'processing' not in data and 'anchors' not in data and 'required_materiality' not in data
            output={'blocks':[{'block_id':block['block_id'],'classification':'material',
                'material_excerpt_ids':block['excerpt_ids'],'reason':'Fonte contém perspectiva qualitativa.'}
                for block in data['blocks']]}
        elif isinstance(data['blocks'],dict):
            output={'blocks':{bid:row(block['excerpt_ids'][0],[]) for bid,block in data['blocks'].items()}}
            slots=payload['response_format']['json_schema']['schema']['properties']['blocks']['properties']
            assert all('anyOf' not in slot and slot['properties']['classification']['enum']==['material']
                for slot in slots.values())
            if bad_first and sum(isinstance(item[1]['blocks'],dict) for item in seen)==1:
                first=next(iter(output['blocks']))
                output['blocks'][first]={'classification':'administrative','reason':'Sem números.','anchors':None}
            if 'errors' in data:
                assert data['errors'][0]['code']=='source_materiality_conflict'
                assert len(data['blocks'])==1
        else:
            output={'blocks':[{'block_id':block['block_id'],'support':'passed','coverage':'passed',
                'relevance':'passed','reason':'Comparação completa contra fonte original.',
                'anchors':[{'anchor_id':anchor['id'],'result':'passed','reason':'Literal.'}
                    for anchor in data['anchors'] if anchor['owner_block_id']==block['block_id']],
                'omission_excerpt_ids':[]} for block in data['blocks']]}
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}],
            'usage':{'prompt_tokens':10,'completion_tokens':10}}
    return transport


def test_source_pass_precedes_extraction_and_is_reused_once_by_full_verifier():
    prepared=source('1. A atividade desacelerou.\n\n2. A incerteza externa permanece elevada.')
    seen=[];session=GroqSession('fake',counter=Counter(),transport=source_responder(seen))
    session.configure('literal-v1','hybrid')
    result=LiteralExtractor(session).extract_prepared(prepared)
    _,evidence=source_artifacts(prepared)
    report=LiteralFactVerifier(session).verify_prepared(prepared,result,LITERAL_POLICY)
    assert report.approved and report.source_materiality_complete and len(report.source_materiality_checks)==2
    assert session.calls==3 and [a['stage'] for a in session.attempt_records]==['verify','extract','verify']
    assert [model for model,_ in seen]==['openai/gpt-oss-120b','openai/gpt-oss-20b','openai/gpt-oss-120b']
    assert sum(data.get('control')=='source_materiality' for _,data in seen)==1
    assert sum(p['requests'] for p in session.plans if p['stage']=='verify')==2
    assert sum(a['prompt_tokens']+a['completion_tokens'] for a in session.attempt_records)==60
    assert len([e for e in session.literal_artifacts if e['stage']=='source_materiality'])==1
    assert session.literal_artifacts[0]['stage']=='source_materiality'


def test_positive_source_conflict_repairs_only_invalid_block_and_keeps_material_schema():
    prepared=source('1. A atividade desacelerou.\n\n2. O risco continua elevado.')
    seen=[];session=GroqSession('fake',counter=Counter(),transport=source_responder(seen,bad_first=True))
    result=LiteralExtractor(session).extract_prepared(prepared)
    assert session.calls==3
    assert result.corrections[0]['preserved_block_ids']==[prepared.blocks[1].block_id]
    assert result.corrections[0]['repair_block_ids']==[prepared.blocks[0].block_id]
    assert all(r.classification=='material' for r in result.processing)


@pytest.mark.parametrize('change',['source','model','events','memo'])
def test_session_memo_is_bound_and_never_silently_reused_for_other_evidence(change):
    prepared=source('1. A atividade desacelerou.')
    seen=[];session=GroqSession('fake',counter=Counter(),transport=source_responder(seen))
    checks=ensure_source_materiality(session,prepared)
    assert ensure_source_materiality(session,prepared)==checks and session.calls==1
    if change=='source':prepared=source('1. O risco diminuiu.')
    elif change=='model':session.profile='single';session.model='openai/gpt-oss-120b'
    elif change=='events':session.literal_artifacts[0]['output']['blocks'][0]['reason']='Alterado'
    else:
        data=json.loads(session._literal_source_materiality_memo['output_json'])
        data['blocks'][0]['reason']='Alterado'
        session._literal_source_materiality_memo['output_json']=json.dumps(data)
    with pytest.raises(ExpectedServiceError,match='outra identidade|alterad|diverge'):
        ensure_source_materiality(session,prepared)
    assert session.calls==1


def test_negative_or_uncertain_source_labels_never_disable_material_alternative():
    from suno_asset.domain.models import SourceMaterialityCheck
    prepared=source('1. A atividade desacelerou.')
    for classification in ('administrative','no_relevant_facts','not_verified'):
        check=SourceMaterialityCheck(block_id=prepared.blocks[0].block_id,classification=classification,
            material_excerpt_ids=(),reason='Parecer negativo ou pendente.')
        schema=_schema_for(EXTRACTION_SCHEMA,prepared,prepared.blocks,source_checks=(check,))
        assert 'anyOf' in schema['properties']['blocks']['properties'][check.block_id]
        wire=_WireExtraction.model_validate_json(json.dumps({'blocks':{check.block_id:row(prepared.blocks[0].excerpt_ids[0],[])}}))
        assert resolve_wire_extraction(prepared,wire,prepared.blocks,source_checks=(check,)).blocks[0].classification=='material'


def test_memo_cannot_be_transferred_to_another_session_even_with_copied_events():
    prepared=source('1. A atividade desacelerou.')
    session=GroqSession('fake',counter=Counter(),transport=source_responder([]))
    ensure_source_materiality(session,prepared)
    other=GroqSession('fake',counter=Counter(),transport=lambda *_:pytest.fail('reject transferred memo before transport'))
    other._literal_source_materiality_memo=deepcopy(session._literal_source_materiality_memo)
    other.literal_artifacts=deepcopy(session.literal_artifacts)
    with pytest.raises(ExpectedServiceError,match='outra identidade'):
        ensure_source_materiality(other,prepared)
    assert other.calls==0


def test_budget_failure_prevents_even_first_source_call_without_increasing_caps():
    prepared=source('1. A atividade desacelerou.')
    session=GroqSession('fake',counter=Counter(),transport=lambda *_:pytest.fail('no call admitted'),max_tokens=1000)
    with pytest.raises(ExpectedServiceError,match='não cabem'):
        LiteralExtractor(session).extract_prepared(prepared)
    assert session.calls==0 and session.max_tokens==1000


def test_partial_source_pass_cannot_silently_replay_requests_in_the_same_session():
    from urllib.error import URLError
    prepared=source('\n\n'.join(f'{i}. A atividade permanece resiliente.' for i in range(1,10)))
    seen=[];respond=source_responder(seen);calls=[]
    def transport(payload,*args):
        calls.append(payload)
        if len(calls)==2:raise URLError('synthetic interruption after one completed batch')
        return respond(payload,*args)
    session=GroqSession('fake',counter=Counter(),transport=transport)
    with pytest.raises(ExpectedServiceError):ensure_source_materiality(session,prepared)
    assert session.calls==2 and len(session.literal_artifacts)==1
    assert not getattr(session,'_literal_source_materiality_memo',None)
    with pytest.raises(ExpectedServiceError) as exc:ensure_source_materiality(session,prepared)
    assert exc.value.code=='source_materiality_incomplete'
    assert session.calls==2 and len(calls)==2 and len(session.literal_artifacts)==1


def test_nullable_selector_union_preserves_exact_scope_and_null_without_nested_anyof():
    from suno_asset.services.literal import _numeric_metadata_catalog
    prepared=source('1. A pesquisa Focus projeta IPCA de 5,2% em 2026.')
    schema=_schema_for(EXTRACTION_SCHEMA,prepared,prepared.blocks)
    for block in prepared.blocks:
        fields=schema['properties']['blocks']['properties'][block.block_id]['anyOf'][0]['properties']['anchors']['items']['properties']['numeric_facts']['items']['properties']
        catalog=_numeric_metadata_catalog(prepared,(block,))
        for field in ('indicator','unit','period','attribution'):
            selected=[item['id'] for item in catalog if item['field']==field]
            assert fields[field+'_id']==({'type':['string','null'],'enum':[*selected,None]} if selected else {'type':'null'})
    empty=source('1. A atividade desacelerou.')
    fields=_schema_for(EXTRACTION_SCHEMA,empty,empty.blocks)['properties']['blocks']['properties'][empty.blocks[0].block_id]['anyOf'][0]['properties']['anchors']['items']['properties']['numeric_facts']['items']['properties']
    assert all(fields[field+'_id']=={'type':'null'} for field in ('indicator','unit','period','attribution'))


def test_planning_records_source_and_comparison_once_without_semantic_memo_or_provider():
    from suno_asset.m7_budget import PlanningSession, PlannedOnly
    prepared=source('1. A atividade desacelerou.')
    session=PlanningSession('fake',counter=Counter(),transport=lambda *_:pytest.fail('planning never transports'))
    with pytest.raises(PlannedOnly):LiteralExtractor(session).extract_prepared(prepared)
    result=SimulatedLiteralExtractor().extract_prepared(prepared)
    with pytest.raises(PlannedOnly):LiteralFactVerifier(session).verify_prepared(prepared,result,LITERAL_POLICY)
    assert [p['stage'] for p in session.plans]==['verify','extract','verify']
    assert not getattr(session,'_literal_source_materiality_memo',None)
    assert not session.literal_artifacts and session.calls==0


def current_source_gate_fixture(tmp_path, *, source_before_extract):
    from test_m7_source_gate import source_run,reconcile
    run=source_run.__wrapped__(tmp_path)
    result_path=Path(run['result_file']);budget_path=Path(run['budget_file'])
    result=json.loads(result_path.read_text());budget=json.loads(budget_path.read_text())
    result['manifest']['prompt_version']=LITERAL_PROMPT_VERSION
    budget['manifest']=result['manifest'];run['manifest']=result['manifest']
    directory=Path(run['audit_dir'])
    artifacts=[(json.loads((directory/f'attempt-{i:03}-started.json').read_text()),
        json.loads((directory/f'attempt-{i:03}-received.json').read_text())) for i in range(1,6)]
    order=[2,1,3,4,5] if source_before_extract else [1,2,3,4,5]
    attempts=[]
    from hashlib import sha256
    for i,old in enumerate(order,1):
        start,response=deepcopy(artifacts[old-1]);attempt=deepcopy(budget['attempts'][old-1])
        attempt['attempt']=i;start['manifest']=result['manifest']
        if attempt['stage']=='verify':
            start['request']['response_format']['json_schema']['name']='suno_verify_'+LITERAL_PROMPT_VERSION.replace('.','_')
        attempt['payload_sha256']=sha256(json.dumps(start['request'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        start['attempt']=attempt;attempts.append(attempt)
        (directory/f'attempt-{i:03}-started.json').write_text(json.dumps(start))
        (directory/f'attempt-{i:03}-received.json').write_text(json.dumps(response))
    budget['attempts']=attempts;result_path.write_text(json.dumps(result));budget_path.write_text(json.dumps(budget));reconcile(run)
    return run


@pytest.mark.parametrize('source_before_extract',[True,False])
def test_current_audit_proves_blind_pass_before_first_extraction(tmp_path,source_before_extract):
    from suno_asset.m7_acceptance import validate_paid_execution
    run=current_source_gate_fixture(tmp_path,source_before_extract=source_before_extract)
    if source_before_extract:
        assert validate_paid_execution(run,require_current_prompt=True).status.value=='approved'
    else:
        with pytest.raises(ValueError,match='antes da extração'):
            validate_paid_execution(run,require_current_prompt=True)
