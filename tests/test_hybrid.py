"""Hybrid contracts, budgets and cache isolation without real inference."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from urllib.error import URLError
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from suno_asset.api import app as api_module
from suno_asset.domain.models import CheckResult, GenerationRequest, ParsedUnit
from suno_asset.services.gemini import GeminiSession
from suno_asset.services.hybrid_content import HybridGenerator, HybridEvaluator
from suno_asset.services.hybrid_facts import (CONTROLS, POLICY, catalog, extract, prepare, verify,
    dependencies, fact_rows, targeted_repair)
from suno_asset.services.hybrid_rules import rule_extract, unresolved_ranges
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.source_preparation import prepare_source

TEXT = ('O Copom decidiu reduzir a taxa básica de juros para 13,75% a.a.\n\n'
    'O Comitê reitera que a magnitude do ciclo de calibração será ajustada à evolução do cenário.\n\n'
    'A atividade econômica doméstica desacelerou gradualmente.\n\n'
    'Os riscos para a inflação continuam elevados.\n\n'
    'As expectativas de inflação permanecem acima da meta.\n\n'
    'O mercado de trabalho permanece aquecido.')
TABLE = ('Projeções de inflação no cenário de referência\n'
    'Variação do IPCA acumulada em quatro trimestres (%)\n'
    'Índice de preços 2026 2027 1º tri 2028\n'
    'IPCA 5,2 3,9 3,2\nIPCA livres 5,2 4,1 3,2\nIPCA administrados 5,3 3,5 3,2\n')
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SUNO_ECONOMY_PROVIDER','gemini')
    monkeypatch.setenv('GEMINI_MODEL','gemini-3.8-flash')
    monkeypatch.setenv('GEMINI_FREE_ONLY','1')
    monkeypatch.setenv('GEMINI_FREE_QUOTA_DIR',str(tmp_path/'quota'))
    for name in ('GEMINI_TPM_LIMIT','GEMINI_RPM_LIMIT','GEMINI_RPD_LIMIT','GEMINI_TPD_LIMIT'):
        monkeypatch.delenv(name,raising=False)


def source(*pages):
    doc = uuid4()
    return prepare_source(tuple(ParsedUnit(document_id=doc,unit_id=f'u{i}',page=i,text=text,
        start=0,end=len(text),parser_version='test-v1') for i,text in enumerate(pages,1)))


def raw(content,usage=True):
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(content)}}],
        **({'usage':{'prompt_tokens':100,'completion_tokens':100,'total_tokens':200}} if usage else {})}


class Transport:
    def __init__(self,*,omit=False,broken=False,invalid_verifier=False,fail_review=False,bad_format=False,
            glossary_anchor=False,omit_generated_anchor=False):
        self.seen=[];self.omit=omit;self.broken=broken;self.invalid_verifier=invalid_verifier
        self.fail_review=fail_review;self.bad_format=bad_format;self.glossary_anchor=glossary_anchor
        self.omit_generated_anchor=omit_generated_anchor

    def __call__(self,payload,key,timeout):
        stage=payload['response_format']['json_schema']['name'].split('_')[1]
        data=json.loads(payload['messages'][1]['content'])
        self.seen.append((stage,data,payload))
        aliases={loc['id']:row['text'] for row in data.get('source_catalog',[]) for loc in row['locations']}
        def literal(row):return aliases[row['excerpt_id']][row['from']:row['to']]
        if stage=='extract':
            repair='issues' in data
            rows=[]
            for section in data['sections']:
                for block in section['blocks']:
                    if block.get('administrative_rule'):continue
                    for eid in block['owned_ids']:
                        ranges=block.get('unresolved_ranges',{}).get(eid,[]) if not repair else [[0,len(aliases[eid])]]
                        for start,end in ranges:
                            text=aliases[eid][start:end].strip()
                            if not text or text in TABLE:continue
                            rows.append({'block_id':block['id'],'tipo':'risk' if 'riscos' in text else 'rationale',
                                'texto_literal':text,'apoio_ids':[eid]})
            if self.omit and not repair and rows:rows=rows[1:]
            if self.broken and not repair and rows:rows[0]['texto_literal']='literal inexistente'
            return raw({'facts':rows,**({'replace_fact_ids':[]} if repair else {})})
        if stage=='verify':
            examined=list(aliases)
            if self.invalid_verifier:examined=examined[:-1]
            issues=[]
            if (self.omit or self.broken) and sum(s=='verify' for s,_,_ in self.seen)==1:
                blocks=[b for section in data['sections'] for b in section['blocks']]
                owner=next(b for b in blocks if any('atividade econômica' in aliases[e] for e in b['owned_ids']))
                issues=[{'block_id':owner['id'],'fact_ids':[],'code':'omission','reason':'atividade ausente'}]
            return raw({'controls':{c:'failed' if c=='coverage' and issues else 'passed' for c in CONTROLS},
                'examined_excerpt_ids':examined,'pending_excerpt_ids':[],'issues':issues})
        if stage in ('generate','refine'):
            facts=data['facts']
            if self.omit_generated_anchor and stage=='generate':facts=facts[:-1]
            blocks=[{'text':literal(f),'anchor_ids':[f['anchor_id']],'glossary_term':''} for f in facts]
            fmt=data['output_format']
            if fmt=='analytical_text':
                # Keep every selected fact in the displayed body.
                sections={'synthesis':blocks[:1],'development':blocks[1:-1] or blocks[:1],'implications':blocks[-1:]}
                if self.glossary_anchor and stage=='generate':
                    sections['synthesis'].append({'text':'Definição educacional retornada pelo modelo.',
                        'anchor_ids':[facts[0]['anchor_id']],'glossary_term':'Copom'})
                if self.bad_format and stage=='generate':
                    sections['synthesis'][0]['text']+=' texto'*510
                return raw(sections)
            if fmt=='informational_carousel':
                return raw({'hook':blocks[:1],'cards':blocks[:4],'conclusion':blocks[-1:]})
            return raw({'hook':blocks[:1],'spoken_blocks':[{**b,'pause_seconds':0} for b in blocks[1:]],'visual_support':blocks[-1:]})
        if stage=='evaluate':
            if self.fail_review:raise URLError('simulated DNS failure')
            return raw({'claims':[{'claim_id':c['claim_id'],'support':'passed','numbers':'passed','citations':'passed','reason':'ok'} for c in data['claims']],
                'facts':[{'anchor_id':f['anchor_id'],'result':'passed','reason':'ok',
                    'claim_ids':[c['claim_id'] for c in data['claims'] if f['anchor_id'] in c['anchor_ids']],
                    **{k:'passed' for k in ('conditions','negations','modality','attribution')}} for f in data['facts']],
                'coherence':'passed','adequacy':'passed','reason':'ok'})
        raise AssertionError(stage)


def session(transport):
    return GeminiSession('offline',transport=transport,free_only=True)


@pytest.mark.parametrize('text,decision',[
    ('O Copom decidiu reduzir a Selic para 13,75% a.a.',True),
    ('Se a inflação cair, o Copom poderia reduzir a Selic para 13,75%.',False),
    ('O Copom não decidiu reduzir a Selic para 13,75%.',False),
    ('A projeção considera Selic de 13,75%.',False),
    ('O analista recomendou reduzir a Selic para 13,75%.',False),
    ('O Copom decidiu manter a Selic em 13,75%, caso o cenário se confirme.',False),
])
def test_rules_only_adopted_unambiguous_decision(text,decision):
    facts,_,_=rule_extract(source(text),'a'*64)
    assert any(f.tipo=='decision' for f in facts)==decision
    assert all(f.provenance=='rule' for f in facts)


def test_rule_sentence_does_not_hide_paragraph_remainder_and_preserves_guidance():
    s=source(TEXT.replace('\n\n',' '))
    facts,_,admin=rule_extract(s,'a'*64)
    ranges=unresolved_ranges(s,facts,admin)
    assert any('atividade econômica' in e.text[start:end] for e in s.excerpts for start,end in ranges.get(e.excerpt_id,[]))
    assert any('será ajustada à evolução do cenário' in f.texto_literal for f in facts)


@pytest.mark.parametrize('day,valid',[('15 e 16',True),('32',False),('17 a 16',False)])
def test_date_rule_validates_calendar_and_range(day,valid):
    f,_,_=rule_extract(source(f'Data: {day} de setembro de 2026'),'a'*64)
    assert bool(f)==valid


def test_vote_rule_keeps_named_vote_without_inventing_unanimity():
    s=source('O Copom decidiu reduzir a Selic para 13,75%.\n\n20. Votaram por essa decisão os seguintes membros do Comitê: Ana, Beto e Carla.')
    f,_,_=rule_extract(s,'a'*64)
    vote=next(f for f in f if f.tipo=='rationale')
    assert 'Ana, Beto e Carla' in vote.texto_literal and 'unanimidade' not in vote.texto_literal
    assert not rule_extract(source('Os membros poderiam votar por essa decisão.'),'a'*64)[0]


@pytest.mark.parametrize('change,expected',[('none',3),('wrapped',0),('same_periods',0),('no_unit',0),('duplicate_row',0)])
def test_tabular_rules_require_full_indicator_period_scenario_grammar(change,expected):
    text=TABLE
    if change=='wrapped':text=text.replace('IPCA livres 5,2','IPCA livres\n5,2')
    if change=='same_periods':text=text.replace('2026 2027','2026 2026')
    if change=='no_unit':text=text.replace('(%)','')
    if change=='duplicate_row':text=text.replace('IPCA livres','IPCA')
    f,_,_=rule_extract(source(text),'a'*64)
    rows=[f for f in f if f.tipo=='number']
    assert len(rows)==expected
    assert all(f.texto_literal in text and f.provenance=='rule' for f in rows)


def test_full_catalog_deduplicates_equal_text_but_keeps_all_occurrences():
    s=source('Inflação elevada.','Inflação elevada.')
    c=catalog(s)
    assert len(c)==1 and len(c[0]['locations'])==2


def test_four_calls_then_two_with_cache_and_zero_with_idempotency(tmp_path,monkeypatch):
    transport=Transport();sessions=[]
    def factory():
        s=session(transport);sessions.append(s);return s
    monkeypatch.setattr(api_module,'make_economy_session',factory)
    store=LocalStorage(tmp_path/'store');client=TestClient(api_module.create_app(storage=store))
    uploaded=client.post('/documents',json={'text':TEXT}).json()
    payload={'source_id':uploaded['source_id'],'architecture':'literal-v1','model_profile':'economy','mode':'real'}
    first=client.post('/generations',json=payload,headers={'Idempotency-Key':'hybrid-first'})
    assert first.status_code==200,first.text
    assert first.json()['status']=='approved',first.text
    assert [stage for stage,_,_ in transport.seen]==['extract','verify','generate','evaluate']
    assert sessions[0].max_calls==14
    cache=first.json()['manifest']['hybrid']['cache']
    assert not cache['hit'] and cache['namespace']=='hybrid-verified-cache-v2'
    for fmt,audience in [('informational_carousel','intermediate'),('short_video_script','advanced')]:
        before=len(transport.seen)
        result=client.post('/generations',json={**payload,'output_format':fmt,'audience':audience},headers={'Idempotency-Key':fmt})
        assert result.status_code==200 and result.json()['status']=='approved',result.text
        assert [s for s,_,_ in transport.seen[before:]]==['generate','evaluate']
        assert result.json()['manifest']['hybrid']['cache']['hit']
        assert sessions[-1].max_calls==14
        if fmt=='informational_carousel':assert len(result.json()['content']['cards'])==4
    before=len(transport.seen)
    restarted=TestClient(api_module.create_app(storage=LocalStorage(tmp_path/'store')))
    replay=restarted.post('/generations',json=payload,headers={'Idempotency-Key':'hybrid-first'})
    assert replay.json()==first.json() and len(transport.seen)==before
    from suno_asset.config import Settings
    from suno_asset import m7_mvp_release
    # Isolate default resolution; this stub is NOT acceptance evidence.
    monkeypatch.setattr(m7_mvp_release,'load_mvp_release',lambda _: {'version':'m7-functional-mvp-release-v2'})
    implicit=TestClient(api_module.create_app(storage=LocalStorage(tmp_path/'store'),settings=Settings(mvp_release_path=tmp_path/'synthetic-release')))
    replay=implicit.post('/generations',json={'source_id':uploaded['source_id'],'mode':'real'},headers={'Idempotency-Key':'hybrid-first'})
    assert replay.json()==first.json() and len(transport.seen)==before
    monkeypatch.setenv('SUNO_ECONOMY_PROVIDER','groq')
    assert restarted.post('/generations',json=payload,headers={'Idempotency-Key':'hybrid-first'}).status_code==409


def test_source_excerpts_and_schema_not_specialized_or_repeated_per_block(tmp_path):
    s=source(TEXT);t=Transport();p=extract(s,session(t),'a'*64)
    assert len(t.seen)==1
    data=t.seen[0][1]
    texts=[row['text'] for row in data['source_catalog']]
    assert len(texts)==len(set(texts))
    assert 'enum' not in json.dumps(t.seen[0][2]['response_format']['json_schema']['schema']['properties']['facts']['items']['properties']['block_id'])
    assert p.verification['passage_routes'] and not p.pending_material_checks


def test_verifier_omission_gets_targeted_repair_and_reverification(tmp_path):
    t=Transport(omit=True);s=source(TEXT)
    p,extraction,report=prepare(s,session(t),'a'*64,tmp_path/'store')
    assert report.approved
    assert [s for s,_,_ in t.seen]==['extract','verify','extract','verify']
    repair=t.seen[2][1]
    assert len([b for sec in repair['sections'] for b in sec['blocks']])==1
    assert 'targeted_repair' in p.verification


def test_invalid_literal_is_not_cached_as_approved_before_repair(tmp_path):
    t=Transport(broken=True)
    p,_,report=prepare(source(TEXT),session(t),'a'*64,tmp_path/'store')
    assert report.approved and not p.pending_material_checks
    assert [s for s,_,_ in t.seen]==['extract','verify','extract','verify']


def test_missing_examined_excerpt_blocks_approval_and_cache(tmp_path):
    t=Transport(invalid_verifier=True)
    with pytest.raises(ExpectedServiceError) as e:prepare(source(TEXT),session(t),'a'*64,tmp_path/'store')
    assert e.value.code=='invalid_hybrid_verification'
    assert not list((tmp_path/'store'/'hybrid-verified-cache-v2').glob('*.json'))


def test_corrupt_cache_reprepares_and_concurrent_preparation_is_shared(tmp_path):
    s=source(TEXT);t=Transport()
    def run():return prepare(s,session(t),'a'*64,tmp_path/'store')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:run(),range(2)))
    assert all(r[2].approved for r in results)
    assert [stage for stage,_,_ in t.seen]==['extract','verify']
    file=next((tmp_path/'store'/'hybrid-verified-cache-v2').glob('*.json'))
    file.write_text('corrupted')
    assert run()[2].approved
    assert [stage for stage,_,_ in t.seen]==['extract','verify','extract','verify']


def test_cache_dependencies_change_with_effective_configuration(tmp_path):
    s=source(TEXT);client=session(Transport())
    first=dependencies(s,'a'*64,client)
    client.resolved_identity={**client.resolved_identity,'reasoning_effort':'high'}
    assert dependencies(s,'a'*64,client)!=first


def test_format_correction_preserves_selected_facts_and_evaluator_failure_retains_draft(tmp_path,monkeypatch):
    for name,transport in [('format',Transport(bad_format=True)),('failure',Transport(fail_review=True))]:
        monkeypatch.setattr(api_module,'make_economy_session',lambda:session(transport))
        client=TestClient(api_module.create_app(storage=LocalStorage(tmp_path/name)))
        uploaded=client.post('/documents',json={'text':TEXT}).json()
        result=client.post('/generations',json={'source_id':uploaded['source_id'],'mode':'real','architecture':'literal-v1','model_profile':'economy'},headers={'Idempotency-Key':name+'-request'})
        assert result.status_code==200,result.text
        row=result.json()
        if name=='format':
            assert row['status']=='approved',result.text
            assert [s for s,_,_ in transport.seen]==['extract','verify','generate','refine','evaluate']
            assert row['content']['revision']==1
        else:
            assert row['status']=='needs_review' and row['evaluation'] is None
            assert row['content'] is not None and row['history'][0]['error']['code']=='gemini_unavailable'


def test_known_glossary_definition_preserves_text_with_accidental_fact_anchor_without_retry(tmp_path,monkeypatch):
    transport=Transport(glossary_anchor=True)
    client=session(transport)
    prepared=source(TEXT)
    _,extraction,report=prepare(prepared,client,'a'*64,tmp_path/'glossary-anchor')
    assert report.approved
    source_request=LocalStorage(tmp_path/'glossary-anchor-storage').put_text(TEXT)
    request=GenerationRequest(request_id=uuid4(),source=source_request,audience='beginner',
        output_format='analytical_text',mode='real',architecture='literal-v1',model_profile='economy')

    content=HybridGenerator(client).generate(request,extraction.anchors,())

    assert 'Contexto educacional — Copom: Comitê de Política Monetária' in content.synthesis
    linked=next(claim for claim in content.claims if claim.text=='Definição educacional retornada pelo modelo.')
    generation_data=next(data for stage,data,_ in transport.seen if stage=='generate')
    assert linked.anchor_ids==(generation_data['facts'][0]['anchor_id'],)
    assert [stage for stage,_,_ in transport.seen]==['extract','verify','generate']


def test_generation_gets_one_bounded_retry_when_selected_anchor_is_missing(tmp_path):
    transport=Transport(omit_generated_anchor=True)
    client=session(transport)
    prepared=source(TEXT)
    _,extraction,report=prepare(prepared,client,'a'*64,tmp_path/'generation-retry')
    source_request=LocalStorage(tmp_path/'generation-retry-storage').put_text(TEXT)
    request=GenerationRequest(request_id=uuid4(),source=source_request,audience='beginner',
        output_format='analytical_text',mode='real',architecture='literal-v1',model_profile='economy')
    content=HybridGenerator(client).generate(request,extraction.anchors,())
    assert {a for claim in content.claims for a in claim.anchor_ids}=={a.anchor_id for a in extraction.anchors}
    assert [stage for stage,_,_ in transport.seen]==['extract','verify','generate','refine']


def test_reviewer_empty_claim_link_becomes_coverage_failure_and_refines(tmp_path):
    transport=Transport()
    modified=False
    def empty_link(payload,*args):
        nonlocal modified
        response=transport(payload,*args)
        if transport.seen[-1][0]=='evaluate' and not modified:
            modified=True
            result=json.loads(response['choices'][0]['message']['content'])
            result['facts'][0]['claim_ids']=[]
            for verdict,claim in zip(result['claims'],transport.seen[-1][1]['claims'],strict=True):
                if not any(character.isdigit() for character in claim['text']):
                    verdict['numbers']='not_verified'
            response['choices'][0]['message']['content']=json.dumps(result)
        return response
    client=session(empty_link)
    prepared=source(TEXT)
    _,extraction,report=prepare(prepared,client,'a'*64,tmp_path/'empty-link-cache')
    assert report.approved
    from suno_asset.services.source_preparation import source_artifacts
    _,evidence=source_artifacts(prepared)
    source_request=LocalStorage(tmp_path/'empty-link-store').put_text(TEXT)
    request=GenerationRequest(request_id=uuid4(),source=source_request,audience='beginner',
        output_format='analytical_text',mode='real',architecture='literal-v1',model_profile='economy')
    generator=HybridGenerator(client)
    content=generator.generate(request,extraction.anchors,evidence)
    from suno_asset.mvp_economy import economy_policy
    evaluator=HybridEvaluator(client)
    failed=evaluator.evaluate(content,extraction.anchors,evidence,economy_policy('gemini'))
    coverage=next(check for check in failed.checks if check.name=='coverage')
    citation=next(check for check in failed.checks if check.name=='citations')
    numbers=next(check for check in failed.checks if check.name=='numbers')
    assert coverage.result==CheckResult.FAILED and citation.result==CheckResult.PASSED
    assert numbers.result==CheckResult.PASSED
    assert all(issue.correctable for issue in failed.issues)
    revised=generator.refine(content,failed.issues,extraction.anchors,evidence,1)
    passed=evaluator.evaluate(revised,extraction.anchors,evidence,economy_policy('gemini'))
    assert passed.approved and revised.revision==1
    assert [stage for stage,_,_ in transport.seen]==['extract','verify','generate','evaluate','refine','evaluate']


def test_pending_controls_and_unlocalized_verification_never_enter_cache(tmp_path):
    t=Transport()
    def transport(payload,*args):
        response=t(payload,*args)
        stage=t.seen[-1][0]
        if stage=='verify':
            data=json.loads(response['choices'][0]['message']['content'])
            data['controls']['conditions']='not_verified'
            response['choices'][0]['message']['content']=json.dumps(data)
        return response
    pack,_,report=prepare(source(TEXT),session(transport),'a'*64,tmp_path/'store')
    assert not report.approved and not pack.verification['approved']
    assert [s for s,_,_ in t.seen]==['extract','verify']
    assert not list((tmp_path/'store'/'hybrid-verified-cache-v2').glob('*.json'))


def test_targeted_repair_cannot_delete_unflagged_valid_facts(tmp_path):
    s=source(TEXT);t=Transport();client=session(t);pack=extract(s,client,'a'*64)
    target=pack.facts[-1]
    issue={'controls':{c:'passed' for c in CONTROLS},'examined_excerpt_ids':[e.excerpt_id for e in s.excerpts],
        'pending_excerpt_ids':[],'issues':[{'block_id':target.owner_block_id,'fact_ids':[], 'code':'omission','reason':'test'}]}
    def malicious(payload,*args):return raw({'replace_fact_ids':[pack.facts[0].id],'facts':[]})
    client.transport=malicious
    repaired=targeted_repair(s,pack,issue,client)
    assert repaired.facts==pack.facts
    assert repaired.verification['ignored_replacement_ids']==[pack.facts[0].id]


def test_cross_page_core_keeps_complete_owned_support_and_selected_dependencies(tmp_path):
    from suno_asset.services.hybrid_facts import make_pack
    from suno_asset.services.hybrid_content import select
    from suno_asset.experiments.literal_efficiency.contracts import normalize_model_facts,adapt_to_legacy
    s=source('1. O Copom decidiu manter a Selic', 'em 13,75% ao ano, conforme a evolução do cenário.')
    block=next(b for b in s.blocks if len(b.excerpt_ids)==2)
    facts=[]
    for i,eid in enumerate(block.excerpt_ids):
        text=next(e.text for e in s.excerpts if e.excerpt_id==eid)
        facts.extend(normalize_model_facts(s,block.block_id,[dict(tipo='decision' if i==0 else 'rationale',texto_literal=text.strip(),apoio_ids=[eid])],source_sha256='a'*64))
    pack=make_pack(s,'a'*64,facts,{})
    anchors=adapt_to_legacy(s,pack).anchors
    chosen,manifest=select(s,pack,anchors,'short_video_script')
    assert len(chosen)==2 and len(manifest['dependency_fact_ids'])==2
    assert all(set(block.excerpt_ids)<=set(e.excerpt_id for e in a.support_excerpts) for a in chosen)


def test_duplicate_or_missing_semantic_fact_verdict_never_approves(tmp_path,monkeypatch):
    t=Transport()
    def bad_review(payload,*args):
        response=t(payload,*args)
        if t.seen[-1][0]=='evaluate':
            result=json.loads(response['choices'][0]['message']['content'])
            result['facts'][-1]=result['facts'][0]
            response['choices'][0]['message']['content']=json.dumps(result)
        return response
    monkeypatch.setattr(api_module,'make_economy_session',lambda:session(bad_review))
    client=TestClient(api_module.create_app(storage=LocalStorage(tmp_path/'store')))
    upload=client.post('/documents',json={'text':TEXT}).json()
    result=client.post('/generations',json={'source_id':upload['source_id'],'mode':'real','architecture':'literal-v1','model_profile':'economy'},headers={'Idempotency-Key':'bad-verdict'})
    assert result.json()['status']=='needs_review' and result.json()['evaluation'] is None
    assert result.json()['errors'][-1]['code']=='invalid_hybrid_review'


def test_fresh_and_cached_attempt_limits_are_enforced_before_transport(tmp_path):
    for limit in (8,4):
        t=Transport();client=session(lambda *_:raw({'ok':True}));client.max_calls=limit
        schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
        for _ in range(limit):client.call('extract','test',{},schema,200)
        with pytest.raises(ExpectedServiceError) as exc:client.call('extract','test',{},schema,200)
        assert exc.value.code=='llm_budget_exceeded' and client.calls==limit


def test_semantic_guidance_classification_becomes_mandatory_only_after_verification(tmp_path):
    from suno_asset.services.hybrid_content import select
    text=TEXT+'\n\nO Comitê antevê que será necessário manter os juros elevados por período prolongado.'
    s=source(text);t=Transport()
    def classify(payload,*args):
        response=t(payload,*args)
        if t.seen[-1][0]=='extract':
            output=json.loads(response['choices'][0]['message']['content'])
            for row in output['facts']:
                if 'antevê' in row['texto_literal']:row['tipo']='guidance'
            response['choices'][0]['message']['content']=json.dumps(output)
        return response
    pack,extraction,report=prepare(s,session(classify),'a'*64,tmp_path/'store')
    assert report.approved
    f=next(f for f in pack.facts if 'antevê' in f.texto_literal)
    _,selection=select(s,pack,extraction.anchors,'short_video_script')
    assert f.id in selection['mandatory_fact_ids'] and f.id in selection['selected_fact_ids']


def test_targeted_role_correction_is_not_undone_by_selection_heuristic(tmp_path):
    from suno_asset.services.hybrid_content import select
    s=source(TEXT);t=Transport()
    original=next(f for f in rule_extract(s,'a'*64)[0] if f.tipo=='outlook')
    def reclassify(payload,*args):
        response=t(payload,*args)
        stage,data,_=t.seen[-1]
        output=json.loads(response['choices'][0]['message']['content'])
        if stage=='verify' and sum(v=='verify' for v,_,_ in t.seen)==1:
            output['controls']['guidance']='failed'
            output['issues']=[{'block_id':original.owner_block_id,'fact_ids':[original.id],
                'code':'guidance_misclassification','reason':'classificar como fundamento'}]
        elif stage=='extract' and 'issues' in data:
            output={'replace_fact_ids':[original.id],'facts':[{'block_id':original.owner_block_id,
                'tipo':'rationale','texto_literal':original.texto_literal,'apoio_ids':list(original.apoio_ids)}]}
        response['choices'][0]['message']['content']=json.dumps(output)
        return response
    pack,extraction,report=prepare(s,session(reclassify),'a'*64,tmp_path/'store')
    assert report.approved and len(t.seen)==4
    repaired=next(f for f in pack.facts if f.texto_literal==original.texto_literal)
    assert repaired.tipo=='rationale' and repaired.id not in pack.verification['guidance_fact_ids']
    _,selection=select(s,pack,extraction.anchors,'short_video_script')
    assert repaired.id not in selection['mandatory_fact_ids']


def test_semantically_corrupt_cache_with_valid_envelope_is_quarantined(tmp_path):
    s=source(TEXT);t=Transport()
    prepare(s,session(t),'a'*64,tmp_path/'store')
    root=tmp_path/'store'/'hybrid-verified-cache-v2'
    path=next(root.glob('*.json'));envelope=json.loads(path.read_text())
    envelope['body']['facts']['factpack']['facts'][0]['start']+=1
    envelope['sha256']=sha256(json.dumps(envelope['body'],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    path.write_text(json.dumps(envelope))
    assert prepare(s,session(t),'a'*64,tmp_path/'store')[2].approved
    assert len(t.seen)==4 and len(list((root/'rejected').glob('*.json')))==1


def test_groq_mvp_acceptance_cannot_activate_gemini(tmp_path):
    from suno_asset.config import Settings
    outputs={}
    for fmt in ('analytical_text','informational_carousel','short_video_script'):
        path=tmp_path/(fmt+'.json');path.write_text('{}')
        outputs[fmt]={'path':path.name,'sha256':sha256(path.read_bytes()).hexdigest()}
    release=tmp_path/'legacy.json'
    release.write_text(json.dumps({'version':'m7-functional-mvp-release-v1','status':'accepted',
        'configuration':{'architecture':'literal-v1','model_profile':'economy'},'quality_review':'approved','outputs':outputs}))
    client=TestClient(api_module.create_app(storage=LocalStorage(tmp_path/'store'),settings=Settings(mvp_release_path=release)))
    response=client.get('/capabilities')
    assert response.status_code==503 and response.json()['detail']['error']=='mvp_release_invalid'


def test_hybrid_acceptance_requires_the_exact_mvp_source(tmp_path):
    from suno_asset.m7_mvp_release import load_mvp_release
    from suno_asset.services.artifact_lifecycle import runtime_fingerprint
    from suno_asset.services.gemini import configuration
    source=tmp_path/'synthetic.txt';source.write_text('synthetic source, not the official minute')
    release=tmp_path/'release.json'
    release.write_text(json.dumps({'version':'m7-functional-mvp-release-v2','status':'accepted','quality_review':'approved',
        'runtime_fingerprint_sha256':runtime_fingerprint(),
        'configuration':{'architecture':'literal-v1','model_profile':'economy','provider':'gemini','models':configuration()['models'],'policy_version':POLICY.version},
        'source':{'path':source.name,'sha256':sha256(source.read_bytes()).hexdigest()},'source_sha256':sha256(source.read_bytes()).hexdigest()}))
    with pytest.raises(ValueError,match='fonte'):load_mvp_release(release)


def test_real_pdf_uses_logical_preparation_not_legacy_groq_block_guard(tmp_path,monkeypatch):
    pdf=REPO/'data/m7-corpus/Copom281.pdf'
    if not pdf.is_file():pytest.skip('fixture oficial local ausente')
    t=Transport();monkeypatch.setattr(api_module,'make_economy_session',lambda:session(t))
    client=TestClient(api_module.create_app(storage=LocalStorage(tmp_path/'store')))
    upload=client.post('/documents',content=pdf.read_bytes(),headers={'Content-Type':'application/pdf','X-Filename':'Copom281.pdf'})
    assert upload.status_code==201
    response=client.post('/generations',json={'source_id':upload.json()['source_id'],'mode':'real','architecture':'literal-v1','model_profile':'economy'},headers={'Idempotency-Key':'official-pdf-offline'})
    assert response.status_code==200,response.text
    assert [stage for stage,_,_ in t.seen]==['extract','verify','generate','evaluate'],response.text
    assert response.json()['manifest']['max_calls']==14
