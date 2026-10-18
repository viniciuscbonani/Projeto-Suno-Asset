"""Batch resumptions use a synthetic corpus and never call a provider."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

import pytest

from suno_asset import m7_literal_eval as module, m7_literal_acceptance as acceptance, m7_promotion as promotion
from suno_asset.m7_eval import _save
from suno_asset.services.groq import GroqSession
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.quota_ledger import FreeQuotaLedger, QuotaWindow


def setup_cli(tmp_path,monkeypatch,*,heldout):
    corpus=tmp_path/'synthetic-corpus.json';frozen=tmp_path/'synthetic-frozen.json'
    source={'id':'synthetic-only','sha256':'f'*64,'split':'heldout' if heldout else 'development'}
    corpus.write_text(json.dumps({'sources':[source]}));frozen.write_text('{}')
    db_path=tmp_path/'quota.sqlite3';ledger=FreeQuotaLedger(db_path)
    monkeypatch.setenv('GROQ_FREE_QUOTA_DB',str(db_path))
    monkeypatch.setenv('GROQ_FREE_QUOTA_EVIDENCE',str(tmp_path/'synthetic-evidence.json'))
    monkeypatch.setattr('suno_asset.m7_acceptance.fingerprint',lambda *_:{'synthetic_fingerprint':1})
    monkeypatch.setattr('suno_asset.services.quota_ledger.validate_free_quota_evidence',lambda *_,**__:{'limits':QuotaWindow().__dict__})
    monkeypatch.setattr(module,'make_session',lambda **_:GroqSession('fake',free_only=True,
        quota_ledger=ledger,quota_evidence={'synthetic':True},transport=lambda *_:pytest.fail('provider not permitted')))
    monkeypatch.setattr(acceptance,'validate_frozen_configuration',lambda *_:{'synthetic':True})
    marker=tmp_path/'synthetic-reserve-ledger.json'
    monkeypatch.setattr(promotion,'reserve_ledger_path',lambda *_:marker)
    def begin(frozen_path,corpus_path,name,out):
        _save(marker,{'status':'started','correction_guided_by_reserve':False,
            'frozen_sha256':sha256(frozen_path.read_bytes()).hexdigest(),'out':str(out.resolve())})
        return marker
    monkeypatch.setattr(promotion,'begin_reserve',begin)
    calls=[]
    def run_case(source,name,out,max_refinements,**kwargs):
        job=kwargs['quota_job_id'];fp=kwargs['fingerprint_sha256'];rep=kwargs['repetition']
        audience=kwargs['audience'];fmt=kwargs['output_format']
        protocol=kwargs['execution_protocol']
        identity,expected=module.execution_identity(source,name,audience,fmt,rep,fp,protocol)
        assert expected==job
        module.register_execution(ledger,identity,job)
        ledger.start_job(job)
        try:reservation=ledger.reserve_attempt(model='synthetic',job_id=job,payload_sha256='a'*64,tokens=100)
        except ExpectedServiceError as exc:
            assert exc.code=='free_quota_deferred'
            ledger.defer_job(job)
            return {'quota_status':'deferred'}
        ledger.settle_attempt(reservation,actual_tokens=0,status='reported')
        calls.append(job)
        result=out/'synthetic-result.json';_save(result,{'synthetic_only':True})
        row={'source_id':source['id'],'configuration_id':name,'configuration':module.configuration(name),
            'execution_protocol':protocol,
            'quota_job_id':job,'quota_fingerprint_sha256':fp,'quota_status':'complete',
            'audience':audience,'output_format':fmt,'status':'approved','errors':[],'technical_complete':True,
            'attempts':1,'prompt_tokens':0,'completion_tokens':0,'estimated_usd':0,'wall_seconds':0,
            'result_file':str(result),'result_sha256':sha256(result.read_bytes()).hexdigest()}
        # Model the historical omission: recovery must restore repetition from
        # the bound ledger identity rather than dropping or merging the run.
        job_path=out/'synthetic-quota-job.json';_save(job_path,row)
        ledger.complete_job(job,job_path,sha256(job_path.read_bytes()).hexdigest())
        return row
    monkeypatch.setattr(module,'run_case',run_case)
    def arguments(out,*,resume=False):
        args=['m7_literal_eval','--corpus',str(corpus),'--out',str(out),'--free-only',
            '--configurations','literal-quality','--repeats','1' if heldout else '3',
            '--max-runs','9' if heldout else '3','--max-cost-usd','1.35' if heldout else '0.45']
        for stage in ('extract','verify','generate','evaluate','refine'):
            args+=['--stage-cost-cap',stage+'=0.06']
        if heldout:args+=['--matrix','--allow-heldout','--frozen',str(frozen)]
        if resume:args+=['--resume-reserve',str(out/'summary.json')]
        monkeypatch.setattr(sys,'argv',args)
    return ledger,marker,calls,arguments


def test_same_reserved_batch_resumes_queued_after_daily_deferral_and_is_idempotent(tmp_path,monkeypatch):
    ledger,marker,calls,arguments=setup_cli(tmp_path,monkeypatch,heldout=True)
    now=datetime.now(timezone.utc)-timedelta(minutes=25)
    reservations=[ledger.reserve_attempt(model='synthetic',job_id='prior',payload_sha256='b'*64,
        tokens=8000,now=now+timedelta(seconds=index*61)) for index in range(25)]
    out=tmp_path/'batch';arguments(out)
    module.main()
    before_marker=marker.read_bytes()
    summary=json.loads((out/'summary.json').read_text())
    assert summary['status']=='quota_deferred' and summary['rows']==[] and not calls
    for reservation in reservations:ledger.settle_attempt(reservation,actual_tokens=0,status='reported')
    arguments(out,resume=True);module.main()
    summary=json.loads((out/'summary.json').read_text())
    assert summary['status']=='completed_pending_review' and len(summary['rows'])==9 and len(calls)==9
    assert all(row['repetition']==1 for row in summary['rows'])
    assert marker.read_bytes()==before_marker
    module.main()
    assert len(calls)==9 and len(json.loads((out/'summary.json').read_text())['rows'])==9


def test_ledger_recovery_preserves_three_distinct_repetitions(tmp_path,monkeypatch):
    ledger,marker,calls,arguments=setup_cli(tmp_path,monkeypatch,heldout=False)
    arguments(tmp_path/'first');module.main()
    assert len(calls)==3
    arguments(tmp_path/'recovered');module.main()
    report=json.loads((tmp_path/'recovered'/'summary.json').read_text())
    assert len(calls)==3 and len(report['rows'])==3
    assert {row['repetition'] for row in report['rows']}=={1,2,3}
    assert not marker.exists()


def test_reserved_resume_rejects_changed_caps_without_releasing_source_again(tmp_path,monkeypatch):
    ledger,marker,calls,arguments=setup_cli(tmp_path,monkeypatch,heldout=True)
    out=tmp_path/'batch';arguments(out);module.main()
    original=marker.read_bytes();arguments(out,resume=True)
    sys.argv[sys.argv.index('--max-cost-usd')+1]='1.36'
    with pytest.raises(SystemExit):module.main()
    assert len(calls)==9 and marker.read_bytes()==original


@pytest.mark.parametrize('setting', ['cap', 'refinements'])
def test_changed_limits_cannot_recover_or_replay_completed_job(tmp_path, monkeypatch, setting):
    ledger, marker, calls, arguments = setup_cli(tmp_path, monkeypatch, heldout=False)
    arguments(tmp_path/'first'); module.main()
    arguments(tmp_path/'different-settings')
    if setting == 'cap':
        sys.argv[sys.argv.index('--stage-cost-cap')+1] = 'extract=0.05'
    else:
        sys.argv += ['--max-refinements', '0']
    with pytest.raises(SystemExit): module.main()
    assert len(calls) == 3


def test_main_and_additional_matrix_have_distinct_jobs(tmp_path, monkeypatch):
    ledger, marker, calls, arguments = setup_cli(tmp_path, monkeypatch, heldout=False)
    arguments(tmp_path/'main'); module.main()
    main_jobs = set(calls)
    arguments(tmp_path/'matrix')
    sys.argv[sys.argv.index('--repeats')+1] = '1'
    sys.argv[sys.argv.index('--max-runs')+1] = '9'
    sys.argv[sys.argv.index('--max-cost-usd')+1] = '1.35'
    sys.argv += ['--matrix']
    module.main()
    matrix_jobs = set(calls) - main_jobs
    assert len(main_jobs) == 3 and len(matrix_jobs) == 9
    assert len(calls) == 12


def test_prior_summary_rejects_different_caps_before_new_calls(tmp_path, monkeypatch):
    ledger, marker, calls, arguments = setup_cli(tmp_path, monkeypatch, heldout=False)
    arguments(tmp_path/'first'); module.main()
    arguments(tmp_path/'second')
    sys.argv += ['--prior-summary', str(tmp_path/'first'/'summary.json')]
    sys.argv[sys.argv.index('--stage-cost-cap')+1] = 'extract=0.05'
    with pytest.raises(SystemExit): module.main()
    assert len(calls) == 3


@pytest.mark.parametrize('status', ['queued', 'running', 'incomplete'])
def test_changed_caps_never_create_another_logical_job(tmp_path, status):
    ledger = FreeQuotaLedger(tmp_path/'quota.sqlite3')
    source = {'id':'synthetic', 'sha256':'f'*64}
    caps = {stage:0.06 for stage in ('extract','verify','generate','evaluate','refine')}
    protocol = module.make_execution_protocol('main', 2, caps)
    identity, job = module.execution_identity(source,'literal-quality','beginner','analytical_text',1,'fp',protocol)
    module.register_execution(ledger, identity, job)
    if status != 'queued': ledger.start_job(job)
    if status == 'incomplete':
        path = tmp_path/'failed.json'; path.write_text('{}')
        ledger.incomplete_job(job, path, sha256(path.read_bytes()).hexdigest())
    changed = module.make_execution_protocol('main', 2, {**caps, 'extract':0.05})
    changed_identity, same_job = module.execution_identity(source,'literal-quality','beginner','analytical_text',1,'fp',changed)
    assert same_job == job
    with pytest.raises(ValueError): module.register_execution(ledger, changed_identity, same_job)
    assert ledger.job(job)['status'] == status
    with ledger._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
