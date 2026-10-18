"""Persistent real-task safety using fake provider transport only."""
from hashlib import sha256
import json

import pytest

from suno_asset import m7_cases
from suno_asset.services.groq import GroqSession
from suno_asset.services.quota_ledger import FreeQuotaLedger, QuotaWindow
from suno_asset.services.protocols import ExpectedServiceError


CASE={'id':'C1','stage':'post_generation','audience':'beginner','output_format':'analytical_text',
    'candidate':'A decisão é 13,75%.','references':[{'fact_id':'C09','page':5,'item':'18'}]}
SOURCE={'id':'ata-test','sha256':'a'*64}
LABEL={'factual_verdict':'passed','audience_verdict':'passed'}
PAGES={5:'18. A decisão é 13,75%.\n19. Acompanhar dados.'}


def session(tmp_path, ledger=None, transport=None):
    def default_transport(*args):
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'factual_verdict':'passed','factual_reason':'fonte','audience_verdict':'passed',
            'audience_reason':'claro'})}}],'usage':{'prompt_tokens':10,'completion_tokens':10}}
    return GroqSession('fake',transport=transport or default_transport,free_only=True,
        quota_ledger=ledger or FreeQuotaLedger(tmp_path/'quota.sqlite3'),quota_evidence={'test':True})


def run(tmp_path, sess):
    return m7_cases.run_persistent_case(CASE,SOURCE,PAGES,LABEL,'openai/gpt-oss-120b',
        tmp_path/'batch','fingerprint-test',session=sess)


def test_completed_case_recovered_without_new_call_and_hashes_checked(tmp_path):
    first=run(tmp_path,session(tmp_path))
    assert first['quota_status']=='complete' and first['usage_complete']
    second=run(tmp_path,session(tmp_path,transport=lambda *_:pytest.fail('replay')))
    assert second==first
    path=next(iter(first['audit_hashes']))
    with open(path,'a') as stream:stream.write(' ')
    with pytest.raises(ValueError,match='Auditoria'):run(tmp_path,session(tmp_path))


def test_unknown_attempt_remains_reserved_and_is_never_replayed(tmp_path):
    sess=session(tmp_path,transport=lambda *_:(_ for _ in ()).throw(TimeoutError('uncertain')))
    first=run(tmp_path,sess)
    assert first['quota_status']=='incomplete'
    assert sess.quota_ledger.snapshot()['unknown_reserved_tokens']>0
    with pytest.raises(ValueError,match='não repetir'):
        run(tmp_path,session(tmp_path,transport=lambda *_:pytest.fail('replay')))


def test_deferral_before_call_returns_queued_and_can_resume_same_job(tmp_path):
    ledger=FreeQuotaLedger(tmp_path/'quota.sqlite3',QuotaWindow(tokens_per_minute=8000,tokens_per_day=8000))
    reservation=ledger.reserve_attempt(model='test',job_id='earlier',payload_sha256='x',tokens=8000)
    first=run(tmp_path,session(tmp_path,ledger,lambda *_:pytest.fail('quota deferral must precede network')))
    assert first['quota_status']=='deferred' and first['next_window']['available_at']
    assert ledger.job(first['quota_job_id'])['status']=='queued'
    ledger.settle_attempt(reservation,actual_tokens=0,status='reported')
    second=run(tmp_path,session(tmp_path,ledger))
    assert second['quota_job_id']==first['quota_job_id'] and second['quota_status']=='complete'


def test_running_task_cannot_resume_after_crash(tmp_path):
    sess=session(tmp_path)
    identity,job_id=m7_cases._task_identity('case',tmp_path,'fingerprint-test',source_id='ata')
    assert m7_cases._start_persistent_task(sess,identity,job_id) is None
    with pytest.raises(ValueError,match='não repetir'):
        m7_cases._start_persistent_task(session(tmp_path),identity,job_id)


def test_real_task_rejects_non_free_session_before_job_registration(tmp_path):
    sess=GroqSession('fake',transport=lambda *_:pytest.fail('network'))
    with pytest.raises(ExpectedServiceError,match='gratuito'):run(tmp_path,sess)
    assert not (tmp_path/'batch').exists()


def test_case_cli_resume_validates_batch_and_reuses_only_completed(tmp_path,monkeypatch):
    import sys
    corpus=tmp_path/'corpus.json';form=tmp_path/'review.json';case_file=tmp_path/'cases.json'
    corpus.write_text(json.dumps({'sources':[{**SOURCE,'split':'development','case_file':'cases.json'}]}))
    form.write_text('{}');case_file.write_text(json.dumps([CASE]))
    monkeypatch.setattr(m7_cases,'validate_review',lambda *_:{'cases':[{'id':'C1',**LABEL}]})
    monkeypatch.setattr(m7_cases,'source_pages',lambda *_:PAGES)
    monkeypatch.setattr(m7_cases,'fingerprint',lambda *_:'fingerprint-test')
    monkeypatch.setattr(m7_cases,'make_session',lambda **_:session(tmp_path))
    arguments=['m7_cases','--form',str(form),'--corpus',str(corpus),'--out',str(tmp_path/'batch'),
        '--models','openai/gpt-oss-120b','--max-cost-usd','1','--max-stage-cost-usd','0.1']
    monkeypatch.setattr(sys,'argv',arguments)
    m7_cases.main()
    before=FreeQuotaLedger(tmp_path/'quota.sqlite3').snapshot()['attempts_24h']
    monkeypatch.setattr(sys,'argv',arguments+['--resume'])
    m7_cases.main()
    assert FreeQuotaLedger(tmp_path/'quota.sqlite3').snapshot()['attempts_24h']==before
    assert json.loads((tmp_path/'batch'/'summary.json').read_text())['status']=='complete'
    case_file.write_text(json.dumps([{**CASE,'candidate':'Alterado.'}]))
    with pytest.raises(SystemExit):m7_cases.main()
    assert FreeQuotaLedger(tmp_path/'quota.sqlite3').snapshot()['attempts_24h']==before


def test_refinement_cli_rejects_changed_batch_before_provider_work(tmp_path,monkeypatch):
    import sys
    from suno_asset import m7_literal_refinement as module
    from suno_asset.m7_literal_eval import configuration
    corpus=tmp_path/'corpus.json';summary=tmp_path/'base.json';out=tmp_path/'refinement'
    out.mkdir();(out/'batch.json').write_text('{}')
    corpus.write_text(json.dumps({'sources':[{**SOURCE,'split':'development'}]}))
    summary.write_text(json.dumps({'fingerprint':'fingerprint-test','rows':[
        {'source_id':SOURCE['id'],'configuration_id':'literal-quality','technical_complete':True}]}))
    monkeypatch.setattr(module,'fingerprint',lambda *_:'fingerprint-test')
    monkeypatch.setattr(module,'make_session',lambda **_:session(tmp_path))
    monkeypatch.setattr('suno_asset.m7_acceptance.validate_paid_execution',lambda *_,**__:True)
    monkeypatch.setattr(module,'run_refinement',lambda *_,**__:pytest.fail('changed batch must block before work'))
    monkeypatch.setattr(sys,'argv',['refinement','--base-summary',str(summary),'--corpus',str(corpus),
        '--source-id',SOURCE['id'],'--configurations','literal-quality','--out',str(out),'--resume',
        '--max-cost-usd','1','--evaluate-stage-cost-usd','0.4','--refine-stage-cost-usd','0.4'])
    with pytest.raises(SystemExit) as exc:module.main()
    assert exc.value.code==2 and not FreeQuotaLedger(tmp_path/'quota.sqlite3').snapshot()['attempts_24h']


def test_reserved_cases_resume_same_marker_across_quota_window(tmp_path,monkeypatch):
    import sys
    corpus=tmp_path/'corpus.json';form=tmp_path/'review.json';case_file=tmp_path/'cases.json'
    frozen=tmp_path/'frozen.json';reserve=tmp_path/'reserve.json';provider_ledger=tmp_path/'reserve-ledger.json'
    out=tmp_path/'reserved-cases'
    frozen.write_text('{}');frozen_sha=sha256(frozen.read_bytes()).hexdigest()
    provider_ledger.write_text(json.dumps({'frozen_sha256':frozen_sha}))
    reserve.write_text(json.dumps({'frozen_sha256':frozen_sha,'reserve_ledger_sha256':sha256(provider_ledger.read_bytes()).hexdigest(),'rows':[{}]*9}))
    corpus.write_text(json.dumps({'sources':[{**SOURCE,'split':'heldout','case_file':'cases.json'}]}))
    form.write_text('{}');case_file.write_text(json.dumps([CASE]))
    monkeypatch.setattr(m7_cases,'validate_review',lambda *_:{'cases':[{'id':'C1',**LABEL}]})
    monkeypatch.setattr(m7_cases,'source_pages',lambda *_:PAGES)
    monkeypatch.setattr(m7_cases,'fingerprint',lambda *_:'fingerprint-test')
    monkeypatch.setattr('suno_asset.m7_literal_acceptance.validate_frozen_configuration',lambda *_:True)
    monkeypatch.setattr('suno_asset.m7_promotion.reserve_ledger_path',lambda *_:provider_ledger)
    ledger=FreeQuotaLedger(tmp_path/'quota.sqlite3',QuotaWindow(tokens_per_minute=8000,tokens_per_day=8000))
    reserved=ledger.reserve_attempt(model='test',job_id='earlier',payload_sha256='x',tokens=8000)
    monkeypatch.setattr(m7_cases,'make_session',lambda **_:session(tmp_path,ledger))
    arguments=['m7_cases','--form',str(form),'--corpus',str(corpus),'--out',str(out),
        '--configurations','literal-quality','--max-cost-usd','1','--max-stage-cost-usd','0.1',
        '--allow-heldout','--frozen',str(frozen),'--reserve-summary',str(reserve)]
    monkeypatch.setattr(sys,'argv',arguments);m7_cases.main()
    marker=tmp_path/'reserve-ledger-cases.json';original_marker=marker.read_bytes()
    assert json.loads((out/'summary.json').read_text())['status']=='deferred'
    ledger.settle_attempt(reserved,actual_tokens=0,status='reported')
    monkeypatch.setattr(sys,'argv',arguments+['--resume']);m7_cases.main()
    assert json.loads((out/'summary.json').read_text())['status']=='complete'
    assert marker.read_bytes()==original_marker
    monkeypatch.setattr(sys,'argv',arguments)
    with pytest.raises(SystemExit):m7_cases.main()


def test_structured_fingerprint_is_canonical_hash_for_sqlite_job_binding(tmp_path):
    identity,job_id=m7_cases._task_identity('case',tmp_path,{'b':2,'a':1},source_id='ata')
    equivalent,equivalent_id=m7_cases._task_identity('case',tmp_path,{'a':1,'b':2},source_id='ata')
    assert identity==equivalent and job_id==equivalent_id
    assert isinstance(identity['fingerprint'],str) and len(identity['fingerprint'])==64
    m7_cases._start_persistent_task(session(tmp_path),identity,job_id)
    assert FreeQuotaLedger(tmp_path/'quota.sqlite3').job(job_id)['fingerprint']==identity['fingerprint']


def test_configuration_case_budget_uses_effective_evaluator_model(tmp_path,monkeypatch):
    import sys
    corpus=tmp_path/'corpus.json';form=tmp_path/'review.json';case_file=tmp_path/'cases.json'
    corpus.write_text(json.dumps({'sources':[{**SOURCE,'split':'development','case_file':'cases.json'}]}))
    form.write_text('{}');case_file.write_text(json.dumps([CASE]))
    monkeypatch.setattr(m7_cases,'validate_review',lambda *_:{'cases':[{'id':'C1',**LABEL}]})
    monkeypatch.setattr(m7_cases,'make_session',lambda **_:pytest.fail('budget must block before provider setup'))
    # 0.008 admits the 20b's nominal ceiling (0.00648), but not the actual
    # quality evaluator 120b ceiling (0.01296), regardless of --models.
    monkeypatch.setattr(sys,'argv',['cases','--form',str(form),'--corpus',str(corpus),
        '--out',str(tmp_path/'batch'),'--models','openai/gpt-oss-20b',
        '--configurations','literal-quality','--max-cost-usd','0.008','--max-stage-cost-usd','0.007'])
    with pytest.raises(SystemExit) as exc:m7_cases.main()
    assert exc.value.code==2 and not (tmp_path/'batch').exists()


def test_new_output_folder_or_cap_cannot_replay_uncertain_logical_task(tmp_path):
    binding={'source_id':'ata','configuration_id':'literal-quality','case_id':'C1','repetition':1}
    first,first_id=m7_cases._task_identity('isolated-case',tmp_path/'first','fingerprint-test',
        **binding,stage_cost_limit=0.1)
    second,second_id=m7_cases._task_identity('isolated-case',tmp_path/'second','fingerprint-test',
        **binding,stage_cost_limit=0.2)
    assert first_id==second_id
    sess=session(tmp_path)
    m7_cases._start_persistent_task(sess,first,first_id)
    with pytest.raises(ValueError,match='outro fingerprint/fonte/configuração'):
        m7_cases._start_persistent_task(session(tmp_path),second,second_id)
    assert FreeQuotaLedger(tmp_path/'quota.sqlite3').snapshot()['jobs']=={'running':1}
