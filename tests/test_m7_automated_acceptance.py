"""Synthetic release artifacts only: these tests never open the real reserve."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from suno_asset import m7_acceptance as acceptance, m7_review as review
from suno_asset import m7_literal_acceptance as literal, m7_literal_eval as evaluation
from suno_asset import m7_promotion as promotion


@pytest.fixture(autouse=True)
def isolated_reserve_registry(tmp_path,monkeypatch):
    monkeypatch.setattr(promotion,'RESERVE_REGISTRY',tmp_path/'reserve-registry')


def save(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def synthetic_pdf(path, text):
    from reportlab.pdfgen.canvas import Canvas
    canvas=Canvas(str(path),invariant=1)
    canvas.drawString(72,720,text);canvas.save()
    return path


def reviewed(form):
    form.update(status='reviewed',reviewed_at='2026-10-12T12:00:00-03:00',
        limitations=['Synthetic test fixture; no model executed.'],
        passes={'source_first':True,'pipeline_comparison':True},unresolved_divergences=[])
    return form


@pytest.fixture
def corpus(tmp_path):
    sources=[]
    for n in (1,2):
        case_file=f'dev-{n}.json'
        cases=[{'id':f'D{n}-{i}', 'source_id':f'dev-{n}', 'stage':'pre_generation', 'split':'development',
            'audience':'beginner','output_format':'analytical_text','candidate':'Synthetic source',
            'references':[{'fact_id':'F1','page':1,'item':'1'}],
            'expected':{'factual_verdict':'passed'}} for i in range(26 if n==1 else 25)]
        save(tmp_path/case_file,cases)
        pdf=synthetic_pdf(tmp_path/f'dev-{n}.pdf',f'1. Synthetic source dev-{n}.')
        sources.append({'id':f'dev-{n}','split':'development','sha256':acceptance.digest(pdf),'local_pdf':str(pdf),'case_file':case_file})
    # The heldout content does not exist. Preparation/freeze must not need it.
    pdf=synthetic_pdf(tmp_path/'synthetic-sealed.pdf','1. Synthetic sealed source; fixture only.')
    sources.append({'id':'sealed','split':'heldout','sha256':acceptance.digest(pdf),'local_pdf':str(pdf),'case_file':'DO_NOT_OPEN.json'})
    return save(tmp_path/'corpus.json',{'sources':sources})


def complete_cases(corpus, *, split='development'):
    form=reviewed(review.prepare_codex_cases(corpus.parent,split=split))
    for row in form['cases']:
        row.update(factual_verdict='passed',rationale='Synthetic fact preserved',
            source_reference='p.1',support_excerpt='Synthetic source')
    for entry in form['glossary']:entry.update(accepted=True,comment='Synthetic acceptance')
    return form


def inventories(corpus, *, split='development'):
    refs={}
    for source in json.loads(corpus.read_text())['sources']:
        if source['split']!=split:continue
        from suno_asset.domain.models import DocumentMetadata
        from suno_asset.services.parsing import PdfPlumberParser
        from suno_asset.services.source_preparation import prepare_source
        from support.source_review import literal_inventory
        raw_path=Path(source['local_pdf'])
        metadata=DocumentMetadata(document_id='00000000-0000-0000-0000-000000000001',
            source_id='00000000-0000-0000-0000-000000000001',origin='upload',sha256=source['sha256'],artifact_ref=str(raw_path))
        prepared=prepare_source(PdfPlumberParser().parse(raw_path.read_bytes(),metadata).units)
        prepared_path=save(corpus.parent/(source['id']+'-prepared.json'),prepared.model_dump(mode='json'))
        inventory=literal_inventory(prepared)
        for entry in inventory['entries']:entry.update(materiality=True,review_status='reviewed')
        inventory.update(status='reviewed',review_type='codex',source_id=source['id'],source_sha256=source['sha256'],
            raw_source_file=str(raw_path),prepared_source_file=str(prepared_path),prepared_source_sha256=acceptance.digest(prepared_path),
            facts=[{'id':'F1','material':True,'block_ids':[b.block_id for b in prepared.blocks],
                'source_reference':'p.1','support_excerpt':'Synthetic source'}])
        path=save(corpus.parent/(source['id']+'-inventory.json'),{
            **inventory})
        refs[source['id']]={'file':str(path),'sha256':acceptance.digest(path)}
    return refs


def test_codex_preparation_does_not_read_reserved_cases_or_fabricate_identity(corpus):
    form=review.prepare_codex_cases(corpus.parent)
    assert len(form['cases'])==51 and form['session_id'] is form['model'] is None
    assert form['unresolved_divergences'] is None
    assert 'DO_NOT_OPEN.json' not in form['dataset_hashes']
    with pytest.raises(ValueError,match='Codex pendente'):review.validate_review(form,corpus.parent)
    reviewed(form)
    with pytest.raises(ValueError,match='inválido'):review.validate_review(form,corpus.parent)


def test_codex_cases_bind_hashes_support_and_resolved_divergences(corpus):
    form=complete_cases(corpus)
    assert len(review.validate_review(form,corpus.parent)['cases'])==51
    broken=deepcopy(form);broken['cases'][0]['factual_verdict']='failed'
    with pytest.raises(ValueError,match='sem resolução'):review.validate_review(broken,corpus.parent)
    broken['cases'][0]['divergence_resolution']='Synthetic corrected pipeline error'
    assert review.validate_review(broken,corpus.parent)['cases'][0]['verdict']=='failed'
    (corpus.parent/'dev-1.json').write_text('[]')
    with pytest.raises(ValueError,match='Hashes'):review.validate_review(form,corpus.parent)


def test_inventory_cannot_drop_material_support_blocks(corpus):
    refs=inventories(corpus)
    hashes={s['id']:s['sha256'] for s in json.loads(corpus.read_text())['sources'] if s['split']=='development'}
    assert review.validate_inventories({'inventories':refs},hashes)=={'dev-1':{'F1'},'dev-2':{'F1'}}
    path=Path(refs['dev-1']['file']);data=json.loads(path.read_text());data['facts'][0]['block_ids']=[]
    save(path,data);refs['dev-1']['sha256']=acceptance.digest(path)
    with pytest.raises(ValueError,match='apoio'):review.validate_inventories({'inventories':refs},hashes)


def test_unknown_account_retention_permits_only_public_validation():
    account={'version':'m7-public-account-v3','status':'recorded','scope':'public_copom_only',
        'retention':'unknown','private_use_authorized':False,
        'limits':{'tokens_per_minute':8000,'requests_per_minute':None,'tokens_per_day':None,'requests_per_day':None},
        'unverified_hypotheses':{'requests':900,'tokens':240000,'model':None,'window':None}}
    assert acceptance.validate_account(account)['retention']=='unknown'
    account['limits']['tokens_per_minute']=240000
    with pytest.raises(ValueError,match='TPM'):acceptance.validate_account(account)
    account['limits']['tokens_per_minute']=8000;account['private_use_authorized']=True
    with pytest.raises(ValueError,match='privado'):acceptance.validate_account(account)


def test_v3_freeze_rejects_historical_human_review_without_reclassifying(corpus,tmp_path):
    human=save(tmp_path/'human.json',{'version':'m7-human-review-v1'})
    with pytest.raises((ValueError,KeyError,TypeError)):
        literal.freeze_configuration('literal-quality',human,human,human,corpus,human)
    assert json.loads(human.read_text())=={'version':'m7-human-review-v1'}


def test_reserve_claim_is_by_source_hash_and_survives_new_frozen_name(corpus,tmp_path,monkeypatch):
    frozen=save(tmp_path/'frozen.json',{'test':True})
    monkeypatch.setattr(promotion,'validate_frozen_configuration',lambda *_:{'configuration':{'id':'literal-quality'}})
    path=promotion.begin_reserve(frozen,corpus,'literal-quality',tmp_path/'round')
    reserve_hash=next(s['sha256'] for s in json.loads(corpus.read_text())['sources'] if s['split']=='heldout')
    assert path.name=='reserve-use-'+reserve_hash+'.json'
    another=save(tmp_path/'another.json',{'test':'new frozen'})
    with pytest.raises(ValueError,match='já usada'):
        promotion.begin_reserve(another,corpus,'literal-hybrid',tmp_path/'another-round')
    copied_directory=tmp_path/'copied-corpus';copied_directory.mkdir()
    copied_corpus=save(copied_directory/'corpus.json',json.loads(corpus.read_text()))
    assert promotion.reserve_ledger_path(copied_corpus)==path
    with pytest.raises(ValueError,match='já usada'):
        promotion.begin_reserve(another,copied_corpus,'literal-quality',tmp_path/'copied-round')
    promotion.mark_reserve_consumed(corpus,'Synthetic correction informed by reserve')
    assert path.with_name(path.stem+'-correction.json').exists()


def test_early_promotion_and_real_command_without_explicit_cost_never_call_provider(corpus,tmp_path,monkeypatch):
    frozen=save(tmp_path/'frozen.json',{})
    pending=save(tmp_path/'pending.json',{'status':'pending'})
    with pytest.raises(ValueError,match='antecipada'):promotion.promote(frozen,pending,corpus)
    monkeypatch.setattr(sys,'argv',['comparison','--corpus',str(corpus),'--out',str(tmp_path/'real')])
    monkeypatch.setattr(evaluation,'make_session',lambda:pytest.fail('paid call'))
    with pytest.raises(SystemExit):evaluation.main()
    assert not (tmp_path/'real').exists()


def test_reserve_requires_exact_nine_combination_batch_before_provider(corpus,tmp_path,monkeypatch):
    monkeypatch.setattr(sys,'argv',['comparison','--corpus',str(corpus),'--allow-heldout',
        '--frozen',str(tmp_path/'invalid.json'),'--max-cost-usd','1','--configurations','literal-quality',
        '--repeats','1','--out',str(tmp_path/'round')])
    monkeypatch.setattr(evaluation,'make_session',lambda:pytest.fail('paid call'))
    with pytest.raises(SystemExit):evaluation.main()
    assert not (tmp_path/'round').exists()


def content_for(audience,output_format):
    from suno_asset.domain.models import GeneratedContent
    from suno_asset.services.content_formats import to_format
    ref={'document_id':'00000000-0000-0000-0000-000000000001','chunk_id':'test-chunk',
        'parser_version':'test','page':1,'start':0,'end':20}
    claims=[{'claim_id':f'T{i}','text':'Selic em 13,75% a.a.' if i==0 else f'Fato sintético {chr(65+i)}.',
        'anchor_ids':['F1'],'evidence_refs':[ref]} for i in range(5)]
    value=GeneratedContent(content_id='00000000-0000-0000-0000-000000000001',revision=0,
        audience=audience,title='Análise da ata do Copom',synthesis=claims[0]['text'],development=' '.join(c['text'] for c in claims[1:]),
        implications=claims[-1]['text'],claims=claims)
    return to_format(value,output_format).model_dump(mode='json')


def paid_result_artifact(tmp_path, *, config_name, source_id, source_sha, content_value, audience, output_format, tag='', stages=('extract','verify','generate','evaluate'),format_refusal=False,purpose='main',current=False):
    from hashlib import sha256
    from types import SimpleNamespace
    from uuid import UUID, NAMESPACE_URL, uuid5
    from suno_asset.domain.models import (AnchorVerdict, BlockVerification, CheckResult, EvaluationCheck, EvaluationIssue,
        EvaluationPolicy, EvaluationReport, ExecutionResult, ExtractionResult, FactVerificationReport,
        PreparedSource, Revision, SourceBlock, SourceExcerpt, ParsedUnit, Anchor)
    from suno_asset.m7_literal_eval import configuration
    config=evaluation.configuration(config_name);literal_arch=config['architecture']=='literal-v1'
    doc=UUID('00000000-0000-0000-0000-000000000001');text='Selic em 13,75% a.a.' if current else 'A'*20
    ref={'document_id':str(doc),'chunk_id':'test-chunk','parser_version':'test','page':1,'start':0,'end':20}
    policy=EvaluationPolicy(version='real-literal-v2' if literal_arch else 'real-m7-v15',
        required_checks=('structure','coverage','support','numbers','citations','adequacy'))
    content=content_value
    content['claims']=[{**claim,'anchor_ids':['F1']} for claim in content['claims']]
    content['revision']=0
    if format_refusal and output_format=='informational_carousel': content['cards']=content['cards'][:4]
    adapter=__import__('pydantic').TypeAdapter(__import__('suno_asset.domain.models',fromlist=['Content']).Content)
    content_model=adapter.validate_python(content)
    checks=tuple(EvaluationCheck(name=name,result=('failed' if name=='structure' else 'not_verified') if format_refusal else 'passed',
        reason='format precheck' if format_refusal else 'fixture') for name in policy.required_checks)
    evaluation_report=EvaluationReport(revision=0,policy=policy,checks=checks,
        critical_facts=({'anchor_id':'F1','result':'not_verified' if format_refusal else 'passed','reason':'format precheck' if format_refusal else 'fixture',
            'claim_ids':[] if format_refusal else [c.claim_id for c in content_model.claims]},),
        issues=((EvaluationIssue(code='format_noncompliant',message='quatro cards',correctable=True),) if format_refusal else ()),
        deferred_due_to_format=format_refusal,approved=not format_refusal)
    excerpt=SourceExcerpt(excerpt_id='E1',unit_id='U1',text=text,ref=ref)
    anchor=Anchor(anchor_id='F1',kind='decision',importance='critical',statement=text,
        evidence_refs=(excerpt.ref,),support_excerpts=(excerpt,),owner_block_id='B1',extraction_version='extraction-v1',
        numeric_facts=({'original':'13,75%','excerpt_ids':['E1'],'indicator':'Selic','unit':'%'},) if current else ())
    extraction=ExtractionResult(anchors=(anchor,),processing=({'block_id':'B1','classification':'material','reason':'fixture',
        'anchor_ids':['F1'],'batch_id':'BATCH1'},))
    verification=FactVerificationReport(policy_version=policy.version,anchor_ids=('F1',),
        checks=(EvaluationCheck(name='processing',result='passed'),),
        block_verifications=(BlockVerification(block_id='B1',support='passed',coverage='passed',relevance='passed',
            reason='fixture',anchors=(AnchorVerdict(anchor_id='F1',result='passed',reason='fixture'),)),),
        processing_complete=True,approved=True)
    prepared=PreparedSource(segmentation_version='logical-source-v1.3',document_id=doc,
        units=(ParsedUnit(unit_id='U1',document_id=doc,page=1,start=0,end=20,text=text,parser_version='test'),),
        excerpts=(excerpt,),blocks=(SourceBlock(block_id='B1',kind='paragraph',excerpt_ids=('E1',)),))
    protocol=evaluation.make_execution_protocol(purpose,2,{stage:.1 for stage in ('extract','verify','generate','evaluate','refine')})
    manifest={'version':'execution-manifest-v2','architecture':config['architecture'],'profile':config['profile'],
        'stage_cost_limits_usd':protocol['stage_cost_limits'],
        'models':config['models'],'policy_version':policy.version,'resolved_configuration':{
            **config,'model_profile':config['profile'],'audience':audience,'output_format':output_format,'mode':'real',
            'execution_protocol':protocol}}
    if current:
        from suno_asset.services.groq import LITERAL_PROMPT_VERSION, PROMPT_VERSION
        manifest['prompt_version']=LITERAL_PROMPT_VERSION if literal_arch else PROMPT_VERSION
    history=(Revision(number=0,content=content_model,evaluation=evaluation_report),)
    precheck_events=[]
    if format_refusal:
        from types import SimpleNamespace
        from suno_asset.services.literal import LiteralEvaluator
        from suno_asset.workflow.graph import LITERAL_POLICY
        evaluator=LiteralEvaluator(SimpleNamespace(literal_artifacts=precheck_events))
        history=tuple(Revision(number=number,content=content_model.model_copy(update={'revision':number}),
            evaluation=evaluator.evaluate(content_model.model_copy(update={'revision':number}),(anchor,),(),LITERAL_POLICY))
            for number in (0,1,2))
        content_model=history[-1].content;evaluation_report=history[-1].evaluation
        stages=('extract','verify','generate','refine','refine')
    pre_generation_events=[]
    source_payload=comparison_payload=source_output=comparison_output=None
    if current and literal_arch:
        from suno_asset.services.literal import block_payload
        source_check={'block_id':'B1','classification':'material','material_excerpt_ids':['E1'],'reason':'Synthetic source-first check'}
        verification=verification.model_copy(update={'source_materiality_complete':True,
            'source_materiality_checks':(__import__('suno_asset.domain.models',fromlist=['SourceMaterialityCheck']).SourceMaterialityCheck.model_validate(source_check),)})
        source_payload={'control':'source_materiality',**block_payload(prepared,prepared.blocks)}
        comparison_payload={**block_payload(prepared,prepared.blocks),
            'processing':[r.model_dump(mode='json') for r in extraction.processing],
            'anchors':[{'id':anchor.anchor_id,'kind':anchor.kind.value,'importance':anchor.importance.value,
                'owner_block_id':anchor.owner_block_id,'excerpt_ids':['E1'],
                'numeric_facts':[f.model_dump(mode='json') for f in anchor.numeric_facts]}]}
        source_output={'blocks':[source_check]}
        comparison_output={'blocks':[r.model_dump(mode='json') for r in verification.block_verifications]}
        pre_generation_events=[{'stage':'source_materiality','block_ids':['B1'],'output':source_output},
            {'stage':'block_verification','output':comparison_output}]
        stages=('verify',*stages)
    stem=f'{config_name}-{source_id}-{output_format}-{tag}'
    result=ExecutionResult(request_id=uuid5(NAMESPACE_URL,stem),architecture=config['architecture'],
        model_profile=config['profile'],manifest=manifest,prepared_source=prepared if literal_arch else None,
        extraction=extraction if literal_arch else None,document_id=doc,mode='real',status='needs_review' if format_refusal else 'approved',reason='fixture',
        policy_version=policy.version,selected_revision=2 if format_refusal else 0,fact_verification=verification,content=content_model,
        evaluation=evaluation_report,references=content_model.references,
        history=history)
    result_path=save(tmp_path/f'{stem}-result.json',result.model_dump(mode='json'))
    save(result_path.with_name(result_path.stem+'-anchors.json'),[anchor.model_dump(mode='json')])
    audit_dir=tmp_path/f'{stem}-audit';audit_dir.mkdir(exist_ok=True)
    attempts=[]
    editorial_events=[]
    refine_revision=0
    prompt_tokens=0 if current else 1
    for index,stage in enumerate(stages,1):
        payload={'model':config['models'][stage],'messages':[],'max_completion_tokens':1}
        response={'usage':{'prompt_tokens':prompt_tokens,'completion_tokens':1}}
        if current and literal_arch and stage=='verify':
            data=source_payload if index==1 else comparison_payload
            output=source_output if index==1 else comparison_output
            payload['messages']=[{'role':'user','content':json.dumps(data)}]
            payload['response_format']={'json_schema':{'name':'suno_verify_'+manifest['prompt_version'].replace('.','_')}}
            response['choices']=[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]
        if format_refusal and stage in {'generate','refine'}:
            if stage=='refine':
                refine_revision+=1
                payload['messages']=[{'role':'user','content':json.dumps({'previous':{'claims':[
                    {'claim_id':claim.claim_id,'text':claim.text,'anchor_ids':list(claim.anchor_ids)}
                    for claim in history[refine_revision-1].content.claims]}})}]
            output={'hook':[],'cards':[],'conclusion':[]}
            response['choices']=[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]
            editorial_events.append({'stage':stage,'revision':refine_revision,'output':output})
        payload_sha=sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        attempt={'attempt':index,'stage':stage,'model':config['models'][stage],'payload_sha256':payload_sha,
            'input_estimate':1,'output_reserved':1,'usage_known':True,'status':'complete',
            'prompt_tokens':prompt_tokens,'completion_tokens':1}
        save(audit_dir/f'attempt-{index:03}-started.json',{'manifest':manifest,'attempt':attempt,'request':payload})
        save(audit_dir/f'attempt-{index:03}-received.json',{'response':response})
        attempts.append(attempt)
    audit_hashes={str(p.resolve()):sha256(p.read_bytes()).hexdigest() for p in audit_dir.glob('*.json')}
    per_stage=evaluation.cost_records(SimpleNamespace(records=attempts))
    budget_path=save(tmp_path/f'{stem}-budget.json',
        {'manifest':manifest,'attempts':attempts,'plans':[],'per_stage':per_stage,'events':[*pre_generation_events,*editorial_events,*precheck_events]})
    return {'execution_protocol':protocol,'configuration_id':config_name,'configuration':config,'source_id':source_id,'source_sha256':source_sha,
        'architecture':config['architecture'],'profile':config['profile'],'model':config['models']['generate'],
        'audience':audience,'output_format':output_format,'status':'needs_review' if format_refusal else 'approved','selected_revision':2 if format_refusal else 0,'errors':[],
        'technical_complete':False,'processing_complete':True,'attempts':len(attempts),'usage_complete':True,
        'prompt_tokens':len(attempts)*prompt_tokens,'completion_tokens':len(attempts),'per_stage':per_stage,
        'estimated_usd':round(sum(row['estimated_usd'] for row in per_stage.values()),6),
        'wall_seconds':1,'manifest':manifest,'result_file':str(result_path.resolve()),
        'result_sha256':acceptance.digest(result_path),'budget_file':str(budget_path.resolve()),
        'budget_sha256':acceptance.digest(budget_path),'audit_dir':str(audit_dir.resolve()),'audit_hashes':audit_hashes}


def synthetic_audit(tmp_path, config_name, tag, *, case=None, pages=None):
    from hashlib import sha256
    config=evaluation.configuration(config_name)
    manifest={'models':config['models']}
    extras={}
    if case is not None:
        from suno_asset.services.groq import GroqSession
        from suno_asset.m7_cases import evaluate_case
        session=GroqSession('synthetic-audit-only',transport=lambda *_:None)
        session.configure(config['architecture'],config['profile'])
        session.preflight=lambda *_,**__:None
        captured={}
        output={'factual_verdict':'passed','factual_reason':'Synthetic source',
            'audience_verdict':'not_applicable' if case['stage']=='pre_generation' else 'passed',
            'audience_reason':'Synthetic audience'}
        def capture(stage,system,data,schema,output_cap=None):
            captured['payload']=session.payload(stage,system,data,schema,output_cap)
            return output
        session.call=capture
        prediction=evaluate_case(case,pages,session).model_dump(mode='json')
        manifest=session.manifest()
        extras={'manifest':manifest,'model':config['models']['evaluate'],'prediction':prediction}
    directory=tmp_path/f'{config_name}-{tag}-audit';directory.mkdir(exist_ok=True)
    stage='evaluate';payload={'model':config['models'][stage],'messages':[]}
    if case is not None:payload=captured['payload']
    payload_sha=sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    attempt={'attempt':1,'stage':stage,'model':config['models'][stage],'payload_sha256':payload_sha,'status':'complete',
        'input_estimate':1,'output_reserved':1,'usage_known':True,'prompt_tokens':1,'completion_tokens':1}
    save(directory/'attempt-001-started.json',{'manifest':manifest,'attempt':attempt,'request':payload})
    response={'usage':{'prompt_tokens':1,'completion_tokens':1}}
    if case is not None:response['choices']=[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]
    save(directory/'attempt-001-received.json',{'response':response})
    return {'audit_dir':str(directory.resolve()),'audit_hashes':{str(p.resolve()):sha256(p.read_bytes()).hexdigest()
        for p in directory.glob('*.json')},'attempts':[attempt],**extras}


def execution_summary_flags(free_only):
    return ({'paid':False,'provider_calls_real':True,'quota_mode':'free_only','authorized_billing_usd':0}
        if free_only else {'paid':True})


def matrix_artifact(corpus,tmp_path,refs, *, split='development', binding=None, config_name='literal-quality',format_refusal=False,free_only=False):
    rows=[]
    for source in json.loads(corpus.read_text())['sources']:
        if source['split']!=split:continue
        for audience,fmt in sorted(literal.COMBINATIONS):
            rows.append(paid_result_artifact(tmp_path,config_name=config_name,source_id=source['id'],
                source_sha=source['sha256'],content_value=content_for(audience,fmt),audience=audience,output_format=fmt,tag=audience,
                purpose='development-matrix' if split=='development' else 'reserve-matrix',
                current=True,format_refusal=format_refusal and audience=='beginner' and fmt=='informational_carousel' and source==next(
                    item for item in json.loads(corpus.read_text())['sources'] if item['split']==split)))
    summary=save(tmp_path/f'matrix-{config_name}-{split}-summary.json',{'matrix':True,'execution_protocol':rows[0]['execution_protocol'],**execution_summary_flags(free_only),'fingerprint':acceptance.fingerprint(corpus),'rows':rows,**(binding or {})})
    form=reviewed(literal.prepare_matrix_review(summary));form['inventories']=refs
    for row in form['rows']:
        row.update(verdict='passed',reason='Synthetic faithful presentation',
            facts=[{'id':'F1','state':'preserved','source_reference':'p.1','support_excerpt':'Synthetic source'}],
            **{k:0 for k in ('unsupported_claims','numeric_errors','attribution_errors','false_approvals','false_rejections')})
        if format_refusal and row.get('audience')=='beginner' and row.get('output_format')=='informational_carousel':
            row.update(format_noncompliant_justified=True,reason='Quatro cards não acomodam os fatos sem omissão',
                divergence_resolution='Revisão independente confirmou que a estrutura obrigatória não cabe com os fatos completos')
    return save(tmp_path/f'matrix-{config_name}-{split}-review.json',form),summary


@pytest.mark.parametrize('matrix_flag', [None, False, 'true', 1])
def test_matrix_requires_explicit_matrix_batch(corpus,tmp_path,matrix_flag):
    matrix,summary=matrix_artifact(corpus,tmp_path,inventories(corpus))
    data=json.loads(summary.read_text());data['matrix']=matrix_flag;save(summary,data)
    form=json.loads(matrix.read_text());form['summary_sha256']=acceptance.digest(summary)
    with pytest.raises(ValueError,match='explicitamente'):
        literal.validate_matrix_review(form,corpus,'literal-quality',split='development')


@pytest.mark.parametrize('shared_identity', ['quota_job_id','result_file','result_sha256','request_id'])
def test_matrix_cannot_reuse_main_execution_identity(corpus,tmp_path,shared_identity):
    matrix,summary=matrix_artifact(corpus,tmp_path,inventories(corpus))
    data=json.loads(summary.read_text());matrix_run=data['rows'][0]
    main=paid_result_artifact(tmp_path,config_name='literal-quality',source_id=matrix_run['source_id'],
        source_sha=matrix_run['source_sha256'],content_value=content_for('beginner','analytical_text'),
        audience='beginner',output_format='analytical_text',tag='independent-main')
    form=json.loads(matrix.read_text())
    assert literal.validate_matrix_review(form,corpus,'literal-quality',split='development',base_runs=[main])['status']=='reviewed'
    if shared_identity=='quota_job_id':
        main['quota_job_id']=matrix_run['quota_job_id']='same-provider-job'
        save(summary,data);form['summary_sha256']=acceptance.digest(summary)
    elif shared_identity=='result_file':
        main['result_file']=str(Path(matrix_run['result_file']).parent/'.'/Path(matrix_run['result_file']).name)
    else:
        base_result=json.loads(Path(main['result_file']).read_text())
        matrix_result=json.loads(Path(matrix_run['result_file']).read_text())
        if shared_identity=='request_id':
            base_result['request_id']=matrix_result['request_id']
            base_result['reason']='Different result bytes but same request'
        else:
            base_result=matrix_result
        save(Path(main['result_file']),base_result)
    with pytest.raises(ValueError,match='execuções adicionais'):
        literal.validate_matrix_review(form,corpus,'literal-quality',split='development',base_runs=[main])


@pytest.mark.parametrize('change', ['missing','main-purpose','different-refinement-limit','different-cost-cap'])
def test_matrix_rejects_missing_or_changed_execution_protocol(corpus,tmp_path,change):
    matrix,summary=matrix_artifact(corpus,tmp_path,inventories(corpus))
    data=json.loads(summary.read_text())
    if change=='missing':data.pop('execution_protocol')
    elif change=='main-purpose':data['execution_protocol']['purpose']='main'
    elif change=='different-refinement-limit':data['execution_protocol']['max_refinements']=0
    else:data['execution_protocol']['stage_cost_limits']['generate']=.2
    save(summary,data)
    form=json.loads(matrix.read_text());form['summary_sha256']=acceptance.digest(summary)
    with pytest.raises(ValueError,match='[Pp]rotocolo'):
        literal.validate_matrix_review(form,corpus,'literal-quality',split='development')


def test_matrix_rejects_missing_material_fact_pending_metric_and_changed_output(corpus,tmp_path):
    matrix,_=matrix_artifact(corpus,tmp_path,inventories(corpus))
    form=json.loads(matrix.read_text())
    assert literal.validate_matrix_review(form,corpus,'literal-quality',split='development')['status']=='reviewed'
    missing=deepcopy(form);missing['rows'][0]['facts']=[]
    with pytest.raises(ValueError,match='Inventário'):literal.validate_matrix_review(missing,corpus,'literal-quality',split='development')
    pending=deepcopy(form);pending['rows'][0]['numeric_errors']=None
    with pytest.raises(ValueError,match='pendentes'):literal.validate_matrix_review(pending,corpus,'literal-quality',split='development')
    output=Path(form['rows'][0]['result_file']);output.write_text(output.read_text()+' ')
    with pytest.raises(ValueError,match='alterad'):literal.validate_matrix_review(form,corpus,'literal-quality',split='development')


def test_matrix_recognizes_only_independently_justified_format_refusal(corpus,tmp_path):
    matrix,_=matrix_artifact(corpus,tmp_path,inventories(corpus),format_refusal=True)
    form=json.loads(matrix.read_text())
    assert literal.validate_matrix_review(form,corpus,'literal-quality',split='development')['status']=='reviewed'
    refusal=next(row for row in form['rows'] if row['audience']=='beginner' and row['output_format']=='informational_carousel')
    refusal['format_noncompliant_justified']=False
    with pytest.raises(ValueError,match='justificativa independente'):
        literal.validate_matrix_review(form,corpus,'literal-quality',split='development')


@pytest.mark.parametrize('broken', ['no_refinements','missing_precheck','wrong_precheck','budget_failure','factual_failure'])
def test_format_refusal_requires_two_real_corrections_and_complete_local_controls(corpus,tmp_path,broken):
    source=json.loads(corpus.read_text())['sources'][0]
    run=paid_result_artifact(tmp_path,config_name='literal-quality',source_id=source['id'],
        source_sha=source['sha256'],content_value=content_for('beginner','informational_carousel'),
        audience='beginner',output_format='informational_carousel',tag='format',format_refusal=True)
    result_path=Path(run['result_file']);budget_path=Path(run['budget_file'])
    result=json.loads(result_path.read_text());budget=json.loads(budget_path.read_text())
    assert 'evaluate' not in {attempt['stage'] for attempt in budget['attempts']}
    assert acceptance.validate_paid_execution(run).status.value=='needs_review'
    if broken=='no_refinements':
        original=result['history'][0]
        result.update(selected_revision=0,content=original['content'],evaluation=original['evaluation'],history=[original])
        run['selected_revision']=0
    elif broken=='missing_precheck':
        budget['events']=[event for event in budget['events'] if event['stage']!='format_precheck']
    elif broken=='wrong_precheck':
        next(event for event in budget['events'] if event['stage']=='format_precheck')['output']['checks']=[]
    elif broken=='budget_failure':
        result['errors']=[{'stage':'refine','code':'llm_budget_exceeded','message':'no reserve'}]
        run['errors']=['llm_budget_exceeded']
    else:
        result['fact_verification']['approved']=False
    save(result_path,result);save(budget_path,budget)
    run.update(result_sha256=acceptance.digest(result_path),budget_sha256=acceptance.digest(budget_path))
    with pytest.raises(ValueError,match='Recusa|Precheck'):acceptance.validate_paid_execution(run)


def test_paid_trace_rejects_missing_or_inconsistent_audit(corpus,tmp_path):
    with pytest.raises(ValueError,match='auditoria obrigatória'):
        acceptance.validate_trace_hashes({'configuration_id':'literal-quality'})
    trace=synthetic_audit(tmp_path,'literal-quality','tamper')
    started=next(Path(trace['audit_dir']).glob('*-started.json'))
    value=json.loads(started.read_text());value['attempt']['model']='openai/gpt-oss-20b'
    save(started,value)
    trace['audit_hashes']={str(p.resolve()):acceptance.digest(p) for p in Path(trace['audit_dir']).glob('*.json')}
    with pytest.raises(ValueError,match='modelo incompatível'):
        acceptance.validate_trace_hashes({'configuration_id':'literal-quality',**trace})


def test_paid_execution_recovers_audited_schema_retry_with_unknown_usage(corpus,tmp_path):
    source=json.loads(corpus.read_text())['sources'][0]
    run=paid_result_artifact(tmp_path,config_name='literal-quality',source_id=source['id'],
        source_sha=source['sha256'],content_value=content_for('beginner','analytical_text'),
        audience='beginner',output_format='analytical_text',tag='retry')
    audit=Path(run['audit_dir'])
    budget_path=Path(run['budget_file'])
    budget=json.loads(budget_path.read_text())
    original=deepcopy(budget['attempts'])
    checkpoints=[(json.loads((audit/f'attempt-{i:03}-started.json').read_text()),
        json.loads((audit/f'attempt-{i:03}-received.json').read_text())) for i in range(1,len(original)+1)]
    for path in audit.glob('*.json'):path.unlink()
    failed={**original[0],'usage_known':False,'status':'http_error','http_status':400,'provider_code':'json_validate_failed'}
    failed.pop('prompt_tokens');failed.pop('completion_tokens')
    save(audit/'attempt-001-started.json',checkpoints[0][0])
    save(audit/'attempt-001-interrupted.json',{'http_status':400,'usage_known':False,
        'provider_response':json.dumps({'error':{'code':'json_validate_failed'}})})
    attempts=[failed]
    for index,(attempt,(started,received)) in enumerate(zip(original,checkpoints),2):
        attempt['attempt']=index;started['attempt']['attempt']=index
        save(audit/f'attempt-{index:03}-started.json',started)
        save(audit/f'attempt-{index:03}-received.json',received)
        attempts.append(attempt)
    budget['attempts']=attempts;save(budget_path,budget)
    run.update(attempts=len(attempts),usage_complete=False,budget_sha256=acceptance.digest(budget_path),
        audit_hashes={str(path.resolve()):acceptance.digest(path) for path in audit.glob('*.json')})
    assert acceptance.validate_paid_execution(run).status.value=='approved'
    assert not run['usage_complete'] and budget['attempts'][0]['usage_known'] is False
    run['usage_complete']=True
    with pytest.raises(ValueError,match='Uso desconhecido'):acceptance.validate_paid_execution(run)


def complete_freeze(corpus,tmp_path,*,free_only=False):
    refs=inventories(corpus)
    cases_form=save(tmp_path/'case-review.json',complete_cases(corpus))
    account=save(tmp_path/'account.json',{'version':'m7-public-account-v3','status':'recorded','scope':'public_copom_only',
        'retention':'unknown','private_use_authorized':False,'limits':{'tokens_per_minute':8000},
        'unverified_hypotheses':{'requests':900,'tokens':240000,'model':None,'window':None}})
    sources=[s for s in json.loads(corpus.read_text())['sources'] if s['split']=='development']
    runs=[]
    for name in evaluation.CONFIGURATIONS:
        for source in sources:
            for repetition in (1,2,3):
                run=paid_result_artifact(tmp_path,config_name=name,source_id=source['id'],source_sha=source['sha256'],
                    content_value=content_for('beginner','analytical_text'),audience='beginner',output_format='analytical_text',tag=str(repetition),current=True)
                run['repetition']=repetition
                # Both reported costs round to the same six-decimal amount
                # for these tiny synthetic calls; time breaks the tie.
                run['wall_seconds']=1 if name=='literal-quality' else 2
                runs.append(run)
    summary=save(tmp_path/'main-summary.json',{'fingerprint':acceptance.fingerprint(corpus),**execution_summary_flags(free_only),'rows':runs})
    form=reviewed(acceptance.prepare_outputs(summary,corpus,automated=True));form['inventories']=refs
    for row in form['rows']:
        for criterion in row['criteria'].values():criterion.update(verdict='passed',reason='Synthetic criterion')
        row.update(critical_fact_inventory_reviewed=True,
            critical_facts=[{'id':'F1','source_reference':'p.1','support_excerpt':'Synthetic source',
                'anchors':'preserved','text':'preserved','reason':'Synthetic preservation'}],
            format_verdict='passed',format_reason='Synthetic valid analytical output',
            independent_metrics={k:0 for k in row['independent_metrics']})
    outputs=save(tmp_path/'outputs-review.json',form)
    from suno_asset.m7_cases import source_pages
    proposals={case['id']:case for source in sources
        for case in json.loads((corpus.parent/source['case_file']).read_text())}
    pages={source['id']:source_pages(source) for source in sources}
    case_rows=[]
    for name in evaluation.CONFIGURATIONS:
        for case in json.loads(cases_form.read_text())['cases']:
            case_rows.append({'id':case['id'],'source_id':case['source_id'],
                'configuration_id':name,'configuration':evaluation.configuration(name),
                'prediction':{'factual_verdict':'passed','audience_verdict':'not_applicable'},
                'human_factual':'passed','human_audience':'not_applicable','error':None,
                **synthetic_audit(tmp_path,name,f'case-{case["id"]}',case=proposals[case['id']],pages=pages[case['source_id']])})
    cases=save(tmp_path/'cases-summary.json',{**execution_summary_flags(free_only),'fingerprint':acceptance.fingerprint(corpus),
        'review_form_sha256':acceptance.digest(cases_form),'rows':case_rows})
    source=sources[0]
    candidate_reviews={}
    quality_refinements=quality_matrix=None
    for candidate in ('literal-quality','literal-hybrid'):
        from test_literal_pipeline import transport, Counter
        from suno_asset.services.groq import GroqSession
        from suno_asset.services.quota_ledger import FreeQuotaLedger
        from suno_asset.m7_literal_refinement import run_refinement
        def flag_missing(value,data):
            for fact in value['facts']:
                if not fact['claim_ids']:
                    fact.update(result='failed',reason='Synthetic controlled number changed')
        session=GroqSession('synthetic-only',transport=transport([],mutate_coverage=flag_missing),
            counter=Counter(),free_only=True,quota_evidence={'synthetic':True},
            quota_ledger=FreeQuotaLedger(tmp_path/f'{candidate}-refinement-quota.sqlite3'))
        base_row=next(row for row in runs if row['configuration_id']==candidate
            and row['source_id']==source['id'] and row['repetition']==1)
        candidate_run=run_refinement(base_row,source,tmp_path/f'{candidate}-refinement',session=session,
            stage_cost_limits={'evaluate':.08,'refine':.04},
            fingerprint_sha256=acceptance.fingerprint(corpus))
        refinement_summary=save(tmp_path/f'{candidate}-refinement-summary.json',
            {'fingerprint':acceptance.fingerprint(corpus),**execution_summary_flags(free_only),'rows':[candidate_run]})
        refinement=reviewed(literal.prepare_refinement_review(refinement_summary,automated=True))
        refinement['inventories']={source['id']:refs[source['id']]}
        for row in refinement['rows']:
            row.update(correction_verified=True,unsupported_after_claims=0,independent_metrics={'numeric_errors':0,'attribution_errors':0},
                material_facts=[{'id':'F1','before':'altered','after':'preserved','source_reference':'p.1',
                    'support_excerpt':'Synthetic source','reason':'Synthetic correction'}])
            for fact in row['facts']:fact.update(before='altered',after='preserved',source_reference='p.1',reason='Synthetic correction')
        refinements=save(tmp_path/f'{candidate}-refinement-review.json',refinement)
        matrix,_=matrix_artifact(corpus,tmp_path,refs,config_name=candidate,free_only=free_only)
        candidate_reviews[candidate]={'refinements':str(refinements.resolve()),'matrix':str(matrix.resolve())}
        if candidate=='literal-quality': quality_refinements,quality_matrix=refinements,matrix
    candidate_reviews_path=save(tmp_path/'candidate-reviews.json',candidate_reviews)
    args=('literal-quality',cases_form,account,outputs,corpus,cases,quality_refinements,quality_matrix,candidate_reviews_path)
    frozen=literal.freeze_configuration(*args)
    return save(tmp_path/'frozen.json',frozen),args


def test_complete_v3_freeze_and_selection_revalidation(corpus,tmp_path):
    frozen,args=complete_freeze(corpus,tmp_path)
    assert literal.validate_frozen_configuration(frozen,corpus,'literal-quality')['criteria']['candidate_complete_approved_runs']==6
    with pytest.raises(ValueError,match='vence seleção'):
        literal.freeze_configuration('literal-hybrid',*args[1:])
    form=json.loads(args[3].read_text());form['rows'][0]['independent_metrics']['false_approvals']=1
    form['rows'][0]['divergence_resolution']='Synthetic known false approval'
    save(args[3],form)
    with pytest.raises(ValueError,match='alterada'):literal.validate_frozen_configuration(frozen,corpus,'literal-quality')


@pytest.mark.parametrize('change',['missing_choices','response_failed','prediction_changed','request_changed'])
def test_case_prediction_must_reconcile_with_actual_complete_response(corpus,tmp_path,change):
    from suno_asset.m7_cases import source_pages
    from hashlib import sha256
    source=next(s for s in json.loads(corpus.read_text())['sources'] if s['split']=='development')
    case=json.loads((corpus.parent/source['case_file']).read_text())[0]
    pages=source_pages(source)
    spec=evaluation.configuration('literal-quality')
    row={'id':case['id'],'source_id':source['id'],'configuration_id':'literal-quality',
        'configuration':spec,'error':None,**synthetic_audit(tmp_path,'literal-quality','adversarial',case=case,pages=pages)}
    directory=Path(row['audit_dir'])
    if change=='prediction_changed':
        row['prediction']['factual_verdict']='failed'
    elif change=='request_changed':
        path=directory/'attempt-001-started.json'
        artifact=json.loads(path.read_text())
        artifact['request']['messages'][1]['content']='{"candidate":"Different source context"}'
        digest=sha256(json.dumps(artifact['request'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        artifact['attempt']['payload_sha256']=digest
        row['attempts'][0]['payload_sha256']=digest
        save(path,artifact)
    else:
        path=directory/'attempt-001-received.json'
        artifact=json.loads(path.read_text())
        if change=='missing_choices':artifact['response'].pop('choices')
        else:
            output=json.loads(artifact['response']['choices'][0]['message']['content'])
            output['factual_verdict']='failed'
            artifact['response']['choices'][0]['message']['content']=json.dumps(output)
        save(path,artifact)
    row['audit_hashes']={str(p.resolve()):acceptance.digest(p) for p in directory.glob('*.json')}
    with pytest.raises(ValueError):
        acceptance.validate_case_execution(row,case,pages,spec)


@pytest.mark.parametrize('prompt',['literal-v1.9',None])
def test_refinement_qualification_never_skips_audit_for_historical_or_missing_prompt(corpus,tmp_path,prompt):
    _,args=complete_freeze(corpus,tmp_path)
    form=json.loads(args[6].read_text())
    summary_path=Path(form['summary_file'])
    summary=json.loads(summary_path.read_text())
    summary['rows'][0]['manifest']['prompt_version']=prompt
    save(summary_path,summary)
    form['summary_sha256']=acceptance.digest(summary_path)
    with pytest.raises(ValueError,match='versão atual'):
        literal.validate_refinement_review(form,corpus,candidate='literal-quality')


def test_refinement_approval_must_match_semantic_provider_response(corpus,tmp_path):
    from suno_asset.domain.models import ExecutionResult
    _,args=complete_freeze(corpus,tmp_path)
    form=json.loads(args[6].read_text())
    run=json.loads(Path(form['summary_file']).read_text())['rows'][0]
    base=ExecutionResult.model_validate_json(Path(run['base_result_file']).read_bytes())
    directory=Path(run['audit_dir'])
    path=sorted(directory.glob('*-received.json'))[-1]
    artifact=json.loads(path.read_text())
    artifact['response']['choices'][0]['message']['content']=json.dumps({
        'coherence':'passed','adequacy':'failed','reason':'Synthetic rejection actually returned'})
    save(path,artifact)
    run['audit_hashes']={str(p.resolve()):acceptance.digest(p) for p in directory.glob('*.json')}
    with pytest.raises(ValueError,match='divergem das respostas'):
        literal.validate_refinement_execution(run,base,evaluation.configuration('literal-quality'))
    summary_path=Path(form['summary_file'])
    summary=json.loads(summary_path.read_text());summary['rows'][0]=run
    save(summary_path,summary)
    form['summary_sha256']=acceptance.digest(summary_path);save(args[6],form)
    replacement=literal.freeze_configuration('literal-hybrid',*args[1:])
    assert replacement['configuration']['id']=='literal-hybrid'


def test_freeze_requires_all_main_runs_to_share_main_protocol(corpus,tmp_path,monkeypatch):
    _,args=complete_freeze(corpus,tmp_path)
    original=literal.validate_outputs(json.loads(args[3].read_text()),corpus)
    for change in ('missing','purpose','refinement-limit','cost-cap'):
        quality=deepcopy(original)
        run=quality['run_rows'][-1]
        if change=='missing':run.pop('execution_protocol')
        else:
            protocol=run['execution_protocol']
            if change=='purpose':protocol['purpose']='development-matrix'
            elif change=='refinement-limit':protocol['max_refinements']=0
            else:protocol['stage_cost_limits']['generate']=.2
            # Keep each row internally consistent: the gate must also compare
            # it to the other seventeen main trials.
            run['manifest']['resolved_configuration']['execution_protocol']=deepcopy(protocol)
            run['manifest']['stage_cost_limits_usd']=deepcopy(protocol['stage_cost_limits'])
        monkeypatch.setattr(literal,'validate_outputs',lambda *unused, result=quality: result)
        with pytest.raises(ValueError,match='[Pp]rotocolo'):
            literal.freeze_configuration(*args)


def test_admissible_cost_rounding_noise_does_not_change_selection_tie(corpus,tmp_path):
    _,args=complete_freeze(corpus,tmp_path)
    outputs_path=args[3]
    form=json.loads(outputs_path.read_text())
    summary_path=Path(form['summary_file'])
    summary=json.loads(summary_path.read_text())
    for row in summary['rows']:
        if row['configuration_id']=='literal-quality':
            row['estimated_usd']+=5e-13
    save(summary_path,summary)
    form['summary_sha256']=acceptance.digest(summary_path);save(outputs_path,form)
    quality=next(r for r in summary['rows'] if r['configuration_id']=='literal-quality')
    hybrid=next(r for r in summary['rows'] if r['configuration_id']=='literal-hybrid')
    assert quality['estimated_usd']>hybrid['estimated_usd']
    assert round(quality['estimated_usd'],6)==round(hybrid['estimated_usd'],6)
    frozen=literal.freeze_configuration('literal-quality',*args[1:])
    ranks=frozen['selection']['eligible_ranks']
    assert ranks['literal-quality'][2]==ranks['literal-hybrid'][2]
    assert ranks['literal-quality'][3]<ranks['literal-hybrid'][3]


def test_free_only_summary_does_not_bypass_simulation_or_missing_audit(corpus,tmp_path):
    _,args=complete_freeze(corpus,tmp_path,free_only=True)
    case_summary=json.loads(args[5].read_text())
    case_summary['provider_calls_real']=False
    save(args[5],case_summary)
    with pytest.raises(ValueError,match='Casos de outra'):literal.freeze_configuration(*args)
    case_summary['provider_calls_real']=True
    case_summary['rows'][0]['audit_hashes']={}
    save(args[5],case_summary)
    with pytest.raises(ValueError,match='auditoria obrigatória'):literal.freeze_configuration(*args)


def test_matrix_failure_removes_primary_winner_and_selects_next_eligible(corpus,tmp_path):
    _,args=complete_freeze(corpus,tmp_path)
    candidate_reviews=json.loads(args[8].read_text())
    quality_matrix=Path(candidate_reviews['literal-quality']['matrix'])
    quality_form=json.loads(quality_matrix.read_text())
    quality_form['rows'][0].update(verdict='failed',reason='Formato não preserva os fatos neste perfil')
    save(quality_matrix,quality_form)
    frozen=literal.freeze_configuration('literal-hybrid',*args[1:])
    assert frozen['configuration']['id']=='literal-hybrid'
    assert 'literal-quality' in frozen['selection']['rejected']
    assert set(frozen['selection']['eligible_ranks'])=={'literal-hybrid'}


@pytest.mark.parametrize('free_only',[False,True])
def test_complete_synthetic_reserve_acceptance_and_promotion(corpus,tmp_path,free_only):
    frozen,_=complete_freeze(corpus,tmp_path,free_only=free_only)
    out=tmp_path/'reserve-round'
    ledger=promotion.begin_reserve(frozen,corpus,'literal-quality',out)
    out.mkdir()
    binding={'frozen_sha256':acceptance.digest(frozen),'reserve_ledger_sha256':acceptance.digest(ledger)}
    # This is a new synthetic file; the real reserved corpus is never opened.
    save(corpus.parent/'DO_NOT_OPEN.json',[{'id':f'H-{i}','source_id':'sealed','stage':'pre_generation','split':'heldout',
        'audience':'beginner','output_format':'analytical_text','candidate':'Synthetic sealed source',
        'references':[{'fact_id':'F1','page':1,'item':'1'}],
        'expected':{'factual_verdict':'passed'}} for i in range(15)])
    matrix,_=matrix_artifact(corpus,out,inventories(corpus,split='heldout'),split='heldout',binding=binding,free_only=free_only)
    cases_review=save(out/'cases-review.json',complete_cases(corpus,split='heldout'))
    cases_out=out/'cases';cases_out.mkdir()
    save(ledger.with_name(ledger.stem+'-cases.json'),{'out':str(cases_out),**binding})
    from suno_asset.m7_cases import source_pages
    source=next(s for s in json.loads(corpus.read_text())['sources'] if s['split']=='heldout')
    proposals={case['id']:case for case in json.loads((corpus.parent/source['case_file']).read_text())}
    pages=source_pages(source)
    rows=[{'id':r['id'],'source_id':'sealed','configuration_id':'literal-quality',
        'configuration':evaluation.configuration('literal-quality'),'human_factual':'passed','human_audience':'not_applicable',
        'prediction':{'factual_verdict':'passed'},'error':None,
        **synthetic_audit(out,'literal-quality',f'reserve-case-{r["id"]}',case=proposals[r['id']],pages=pages)}
        for r in json.loads(cases_review.read_text())['cases']]
    cases_summary=save(cases_out/'summary.json',{**execution_summary_flags(free_only),'fingerprint':acceptance.fingerprint(corpus),
        'review_form_sha256':acceptance.digest(cases_review),'rows':rows,**binding})
    accepted=save(out/'acceptance.json',promotion.accept_reserve(frozen,matrix,cases_review,cases_summary,corpus))
    promoted=save(out/'promoted.json',promotion.promote(frozen,accepted,corpus))
    assert promotion.load_promoted_configuration(promoted)['id']=='literal-quality'
    promotion.mark_reserve_consumed(corpus,'Synthetic correction using reserve feedback')
    with pytest.raises(ValueError,match='correção'):promotion.load_promoted_configuration(promoted)


def test_fabricated_promotion_files_cannot_activate_api(corpus,tmp_path):
    fake_frozen=save(tmp_path/'fake-frozen.json',{})
    fake_acceptance=save(tmp_path/'fake-acceptance.json',{'status':'accepted','configuration':evaluation.configuration('literal-quality'),
        'artifact_hashes':{},'inputs':{'ledger':str(tmp_path/'fake-ledger.json')}})
    path=save(tmp_path/'fake-promoted.json',{'version':'m7-promotion-v3','status':'promoted',
        'configuration':evaluation.configuration('literal-quality'),'frozen_file':str(fake_frozen),
        'frozen_sha256':acceptance.digest(fake_frozen),'acceptance_file':str(fake_acceptance),
        'acceptance_sha256':acceptance.digest(fake_acceptance),'code_hashes':acceptance.fingerprint(corpus)['code_hashes'],
        'corpus_file':str(corpus),'promoted_at':'2026-10-12T12:00:00-03:00'})
    with pytest.raises(ValueError,match='antecipada'):promotion.load_promoted_configuration(path)


def test_preparing_output_rejects_change_since_run_before_review(corpus,tmp_path):
    _,args=complete_freeze(corpus,tmp_path)
    form=json.loads(args[3].read_text());path=Path(form['rows'][0]['result_file'])
    path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError,match='desde a execução'):
        acceptance.prepare_outputs(Path(form['summary_file']),corpus,automated=True)


def test_counted_false_approval_disqualifies_otherwise_approved_candidate(corpus,tmp_path):
    _,args=complete_freeze(corpus,tmp_path)
    form=json.loads(args[3].read_text())
    row=next(r for r in form['rows'] if r['configuration_id']=='literal-quality')
    row['independent_metrics']['false_approvals']=1
    row['divergence_resolution']='Synthetic resolution confirms a factual false approval'
    save(args[3],form)
    with pytest.raises(ValueError,match='elegível|vence seleção'):literal.freeze_configuration(*args)


def test_refinement_requires_actual_refine_attempt_and_material_preservation(corpus,tmp_path):
    _,args=complete_freeze(corpus,tmp_path)
    base_runs=acceptance.validate_outputs(json.loads(args[3].read_text()),corpus)['run_rows']
    form=json.loads(args[6].read_text())
    summary=Path(form['summary_file']);value=json.loads(summary.read_text())
    valid_attempts=deepcopy(value['rows'][0]['attempts'])
    value['rows'][0]['attempts']=[{'stage':'evaluate','status':'complete'}]
    save(summary,value);form['summary_sha256']=acceptance.digest(summary)
    with pytest.raises(ValueError,match='Tentativas|[Rr]efinamento real'):
        literal.validate_refinement_review(form,corpus,candidate='literal-quality',base_runs=base_runs)
    value['rows'][0]['attempts']=valid_attempts
    save(summary,value);form['summary_sha256']=acceptance.digest(summary)
    form['rows'][0]['material_facts'][0]['after']='omitted'
    with pytest.raises(ValueError,match='todo inventário'):
        literal.validate_refinement_review(form,corpus,candidate='literal-quality',base_runs=base_runs)


def test_format_infeasibility_cannot_hide_other_technical_errors(corpus,tmp_path):
    matrix,summary=matrix_artifact(corpus,tmp_path,inventories(corpus))
    report=json.loads(summary.read_text());run=report['rows'][0]
    run.update(errors=['budget_exceeded'],status='needs_review',technical_complete=False)
    save(summary,report)
    form=json.loads(matrix.read_text());form['summary_sha256']=acceptance.digest(summary)
    form['rows'][0].update(format_noncompliant_justified=True,divergence_resolution='Synthetic refusal')
    with pytest.raises(ValueError,match='tecnicamente incompleta'):
        literal.validate_matrix_review(form,corpus,'literal-quality',split='development')
