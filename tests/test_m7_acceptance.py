"""Independent judgments and unchanged artifacts are required for release."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from suno_asset import m7_acceptance as acceptance, m7_cases, m7_eval
from suno_asset.services.groq import GroqSession
from suno_asset.services.glossary import GLOSSARY_VERSION
from suno_asset.services.llm_budget import TokenCounter
from suno_asset.services.protocols import ExpectedServiceError

CORPUS=Path('tests/fixtures/evaluation/corpus.json')


def fixture_outputs(tmp_path):
    # Fully synthetic human judgments used only in this test directory.
    source=json.loads(CORPUS.read_text())['sources'][0]
    content={'content_id':'00000000-0000-0000-0000-000000000001','revision':0,
        'audience':'beginner','output_format':'analytical_text','title':'Ata',
        'synthesis':'Decisão','development':'Contexto','implications':'Postura','claims':[]}
    result={'selected_revision':1,'history':[
        {'number':0,'content':content,'evaluation':{'approved':False}},
        {'number':1,'content':{**content,'revision':1},'evaluation':{'approved':True}}]}
    result_path=tmp_path/'result.json';result_path.write_text(json.dumps(result))
    result_path.with_name('result-anchors.json').write_text('[]')
    summary_path=tmp_path/'summary.json';summary_path.write_text(json.dumps({'rows':[
        {'source_id':source['id'],'source_sha256':source['sha256'],'model':'openai/gpt-oss-120b',
            'result_file':str(result_path),'errors':[],'repetition':1}]}))
    form=acceptance.prepare_outputs(summary_path,CORPUS)
    return form,result_path


def complete_fixture(form):
    form.update(status='reviewed',reviewer='synthetic-test-only',reviewed_at='2026-10-06T12:00:00-03:00',
        glossary_version=GLOSSARY_VERSION)
    for row in form['rows']:
        for criterion in row['criteria'].values(): criterion.update(verdict='passed',reason='julgamento sintético')
        row['critical_fact_inventory_reviewed']=True
        for fact in row['critical_facts']:
            fact.update(source_reference='página de teste',anchors='preserved',text='preserved',reason='teste')
    return form


def test_output_preparation_leaves_every_judgment_pending_and_binds_artifacts(tmp_path):
    form,result_path=fixture_outputs(tmp_path)
    assert form['status']=='pending' and len(form['rows'])==2
    assert all(c['verdict'] is None for r in form['rows'] for c in r['criteria'].values())
    with pytest.raises(ValueError,match='humana pendente'): acceptance.validate_outputs(form,CORPUS)
    complete_fixture(form)
    assert acceptance.validate_outputs(form,CORPUS)['reviewed_revisions']==2
    result_path.write_text(result_path.read_text()+' ')
    with pytest.raises(ValueError,match='Metadados|ausentes'): acceptance.validate_outputs(form,CORPUS)


def test_human_omission_and_regression_cannot_be_hidden_by_automatic_approval(tmp_path):
    form,_=fixture_outputs(tmp_path);complete_fixture(form)
    form['rows'][1]['critical_facts'][0]['text']='omitted'
    report=acceptance.validate_outputs(form,CORPUS)
    assert report['critical_false_approvals']==report['false_approvals']==1
    assert report['refinement_regressions']==[form['rows'][1]['id']]
    assert report['models']['openai/gpt-oss-120b']['critical_facts_preserved']==23


def test_fabricated_freeze_and_pending_account_are_rejected(tmp_path):
    account=json.loads(Path('tests/fixtures/evaluation/groq-account-review.json').read_text())
    with pytest.raises(ValueError,match='humana pendente'): acceptance.validate_account(account)
    account.update(status='reviewed',reviewer='synthetic-test-only',verified_at='2026-10-06T12:00:00-03:00',
        scope_description='escopo fictício',zdr_enabled=False,effective_retention_description='regime fictício',
        tpm_limit=8000,split_input_output_limits=False,settings_evidence_description='teste',
        accepted_for_public_copom_use=True,rationale='teste')
    assert acceptance.validate_account(account)['zdr_enabled'] is False
    frozen=tmp_path/'frozen.json'
    frozen.write_text(json.dumps({'status':'frozen','model':'openai/gpt-oss-120b','fingerprint':acceptance.fingerprint(CORPUS)}))
    with pytest.raises(ValueError,match='ausentes'): acceptance.validate_frozen(frozen,CORPUS,['openai/gpt-oss-120b'])
    with pytest.raises(ValueError,match='modelo'): acceptance.validate_frozen(frozen,CORPUS,['openai/gpt-oss-20b'])


def test_pending_case_review_never_contacts_provider(tmp_path,monkeypatch):
    monkeypatch.setattr(sys,'argv',['m7_cases','--form','tests/fixtures/evaluation/human-review-form.json',
        '--out',str(tmp_path/'cases'),'--max-runs','500'])
    monkeypatch.setattr(m7_cases,'make_session',lambda: pytest.fail('no paid call'))
    with pytest.raises(SystemExit) as exc: m7_cases.main()
    assert exc.value.code==2 and not (tmp_path/'cases').exists()


def test_unreviewed_or_fabricated_reserve_never_contacts_provider(tmp_path,monkeypatch):
    frozen=tmp_path/'frozen.json';frozen.write_text('{}')
    monkeypatch.setattr(sys,'argv',['m7_eval','--allow-heldout','--frozen',str(frozen),
        '--out',str(tmp_path/'run'),'--max-runs','100'])
    monkeypatch.setattr(m7_eval,'make_session',lambda: pytest.fail('no paid call'))
    with pytest.raises(SystemExit) as exc: m7_eval.main()
    assert exc.value.code==2 and not (tmp_path/'run').exists()


def test_isolated_case_does_not_send_expected_labels_or_human_rationale():
    seen=[]
    def transport(payload,*_):
        seen.append(payload)
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'factual_verdict':'passed','factual_reason':'fonte','audience_verdict':'passed','audience_reason':'claro'})}}],
            'usage':{'prompt_tokens':10,'completion_tokens':10}}
    case={'stage':'post_generation','audience':'beginner','output_format':'analytical_text',
        'candidate':'A decisão é 13,75%.','references':[{'fact_id':'C09','page':5,'item':'18'}],
        'expected':{'factual_verdict':'failed','rationale':'SECRET_EXPECTED_LABEL'}}
    result=m7_cases.evaluate_case(case,{5:'18. A decisão é 13,75%.\n19. Acompanhar dados.'},
        GroqSession('fake',transport=transport))
    assert result.factual_verdict=='passed'
    assert 'SECRET_EXPECTED_LABEL' not in json.dumps(seen)
    case.update(evidence_availability='unavailable')
    result=m7_cases.evaluate_case(case,{},GroqSession('fake',transport=transport))
    assert result.factual_verdict=='not_verified' and result.audience_verdict=='passed'


def test_case_source_retains_table_or_cross_page_continuation():
    case={'references':[{'fact_id':'C05','page':4,'item':'13'},
        {'fact_id':'C05','page':5,'item':'13 continuação'}, {'fact_id':'C12','page':6,'item':'Tabela 1'}]}
    pages={4:'12. Contexto.\n13. Há riscos de alta e de baixa,\n',
        5:'incluindo a condição que segue na outra página.\n14. Outros dados.',
        6:'Variação do IPCA (%)\nÍndice 2026\nIPCA 5,2\nIPCA livres 5,3'}
    rows=m7_cases.case_evidence(case,pages)
    assert rows[1]['text']==pages[5] and rows[2]['text']==pages[6]
    assert 'riscos de alta e de baixa' in rows[0]['text']


def test_validated_freeze_binds_review_cases_and_current_code(tmp_path,monkeypatch):
    # This entire release fixture is synthetic and lives only under pytest tmp.
    corpus=json.loads(CORPUS.read_text());sources=[s for s in corpus['sources'] if s['split']=='development']
    run_rows=[]
    for model in ('openai/gpt-oss-20b','openai/gpt-oss-120b'):
        for source in sources:
            for repetition in (1,2,3):
                path=tmp_path/f'{source["id"]}-{model.rsplit("/",1)[-1]}-{repetition}.json'
                history=[{'number':r,'content':{'revision':r,'synthetic':True},'evaluation':{'approved':r==1}} for r in (0,1)]
                path.write_text(json.dumps({'request_id':path.stem,'selected_revision':1,'history':history}))
                path.with_name(path.stem+'-anchors.json').write_text('[]')
                run_rows.append({'source_id':source['id'],'source_sha256':source['sha256'],'model':model,
                    'result_file':str(path),'errors':[],'repetition':repetition})
    summary=tmp_path/'summary.json';summary.write_text(json.dumps({'rows':run_rows,'fingerprint':acceptance.fingerprint(CORPUS)}))
    outputs=acceptance.prepare_outputs(summary,CORPUS);complete_fixture(outputs)
    for row in outputs['rows']:
        if not row['critical_facts']:
            row['critical_facts']=[{'id':'synthetic-C01','source_reference':'synthetic source p.5',
                'anchors':'preserved','text':'preserved','reason':'synthetic'}]
        if row['revision']==0: row['criteria']['adequacy'].update(verdict='failed',reason='synthetic initial jargon')
    outputs_path=tmp_path/'outputs.json';outputs_path.write_text(json.dumps(outputs))
    form=json.loads(Path('tests/fixtures/evaluation/human-review-form.json').read_text())
    proposals={row['id']:row for source in corpus['sources'] for row in json.loads((CORPUS.parent/source['case_file']).read_text())}
    form.update(status='reviewed',reviewer='synthetic-test-only',reviewed_at='2026-10-06T12:00:00-03:00',
        glossary_version=GLOSSARY_VERSION)
    for row in form['cases']:
        p=proposals[row['id']]
        row.update(factual_verdict=p['expected']['factual_verdict'],rationale='synthetic label')
        if p['stage']=='post_generation': row['audience_verdict']=p['expected']['audience_verdict']
    for entry in form['glossary']: entry.update(accepted=True,comment='synthetic acceptance')
    form_path=tmp_path/'form.json';form_path.write_text(json.dumps(form))
    account={'status':'reviewed','reviewer':'synthetic-test-only','verified_at':'2026-10-06T12:00:00-03:00',
        'scope_description':'synthetic org','zdr_enabled':False,'effective_retention_description':'synthetic',
        'settings_evidence_description':'synthetic verification','rationale':'synthetic',
        'tpm_limit':8000,'split_input_output_limits':False,'accepted_for_public_copom_use':True}
    account_path=tmp_path/'account.json';account_path.write_text(json.dumps(account))
    cases_path=tmp_path/'cases.json';cases_path.write_text(json.dumps({'fingerprint':acceptance.fingerprint(CORPUS),
        'review_form_sha256':acceptance.digest(form_path),'rows':[
            {'id':row['id'],'model':model,'prediction':{'factual_verdict':row['factual_verdict']},
                'human_factual':row['factual_verdict'],'error':None}
            for model in ('openai/gpt-oss-20b','openai/gpt-oss-120b') for row in form['cases']
            if proposals[row['id']]['split']=='development']}))
    frozen=acceptance.freeze('openai/gpt-oss-120b',form_path,account_path,outputs_path,CORPUS,cases_path)
    path=tmp_path/'frozen.json';path.write_text(json.dumps(frozen))
    assert acceptance.validate_frozen(path,CORPUS,['openai/gpt-oss-120b'])['status']=='frozen'
    cases_path.write_text(cases_path.read_text()+' ')
    with pytest.raises(ValueError,match='alterada'): acceptance.validate_frozen(path,CORPUS,['openai/gpt-oss-120b'])
    # Even equivalent JSON changed after release is detected through its hash.
    cases_path.write_text(cases_path.read_text().rstrip())
    monkeypatch.setattr(acceptance,'fingerprint',lambda _: {'different_code':True})
    with pytest.raises(ValueError,match='código/corpus mudou'): acceptance.validate_frozen(path,CORPUS,['openai/gpt-oss-120b'])
