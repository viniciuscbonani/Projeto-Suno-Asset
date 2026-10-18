"""Offline safety checks for the single-use, free development worker."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from suno_asset.m7_acceptance import CODE_FILES
from suno_asset.services.quota_ledger import FreeQuotaLedger, QuotaWindow
from suno_asset.services.protocols import ExpectedServiceError


@pytest.fixture
def worker(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'scripts/m7_wait_free_pilot.py'
    spec = importlib.util.spec_from_file_location('m7_wait_test_worker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(module, 'fingerprint_hash', lambda: 'fixed')
    # Tests cannot contact either the provider or real credentials.
    monkeypatch.setattr(module.httpx, 'get', lambda *a, **kw: pytest.fail('unexpected HTTP'))
    monkeypatch.setattr(module, 'refresh_evidence', lambda *a: pytest.fail('unexpected refresh'))
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **kw: pytest.fail('unexpected execution'))
    return module


@pytest.fixture
def binding(tmp_path):
    evidence = tmp_path / 'evidence.json'
    evidence.write_text(json.dumps({'account_plan':'free','zero_billing_confirmed':True,
                                    'limits':QuotaWindow().__dict__}))
    return tmp_path / 'queue', evidence, tmp_path / 'quota.sqlite'


def args(queue):
    return SimpleNamespace(queue_dir=queue, fingerprint='fixed')


def seed_usage(ledger, tokens, *, reported):
    """Populate daily usage outside the minute window without contacting Groq."""
    first = datetime.now(timezone.utc) - timedelta(hours=2)
    attempts = []
    while tokens:
        amount = min(tokens, ledger.window.tokens_per_minute)
        attempt = ledger.reserve_attempt(model='openai/gpt-oss-20b', job_id='other-profile',
            payload_sha256='other-request', tokens=amount,
            now=first + timedelta(minutes=2 * len(attempts)))
        if reported:
            ledger.settle_attempt(attempt, actual_tokens=amount, status='reported')
        attempts.append(attempt)
        tokens -= amount
    return attempts, first


def test_worker_is_part_of_candidate_fingerprint():
    assert 'scripts/m7_wait_free_pilot.py' in CODE_FILES


def test_changed_fingerprint_never_starts(worker, binding, monkeypatch):
    queue, evidence, db = binding
    lock, path, state = worker.claim_queue(queue, 'fixed', evidence, db)
    try:
        monkeypatch.setattr(worker, 'fingerprint_hash', lambda:'changed')
        worker.run_worker(args(queue), {}, FreeQuotaLedger(db), path, state)
        saved = json.loads(path.read_text())
        assert saved['status']=='code_changed'
        assert saved['provider_execution_started'] is False
        assert saved['worker_script_sha256']==worker.worker_script_hash()
    finally:
        lock.close()


@pytest.mark.parametrize('reported', [False, True])
def test_exhausted_shared_quota_waits_without_clearing_usage(worker, binding, monkeypatch, reported):
    queue, evidence, db = binding
    ledger = FreeQuotaLedger(db)
    now = datetime.now(timezone.utc)
    attempts, first = seed_usage(ledger, 200000, reported=reported)
    lock, path, state = worker.claim_queue(queue, 'fixed', evidence, db)
    try:
        monkeypatch.setattr(worker.time, 'sleep', lambda *_: worker.stop())
        worker.run_worker(args(queue), {}, ledger, path, state)
        saved = json.loads(path.read_text())
        assert saved['status']=='stopped_before_execution'
        assert saved['quota']['unknown_reserved_tokens']==(0 if reported else 200000)
        assert saved['remaining_tokens']==0
        # Only the first release is required, rather than an empty daily window.
        assert datetime.fromisoformat(saved['next_window_available_at']) == first+timedelta(days=1)
        with ledger._connect() as conn:
            row=conn.execute('SELECT * FROM attempts WHERE id=?',(attempts[0],)).fetchone()
        assert row['status']==('reported' if reported else 'reserved')
        assert row['actual_tokens']==(8000 if reported else None)
        assert row['reserved_tokens']==8000
        # Expiration affects eligibility only, never deletes unknown reservations.
        assert worker.quota_state(ledger,now+timedelta(days=2))[1]==0
        with ledger._connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==len(attempts)
    finally:
        lock.close()


def test_duplicate_worker_and_live_prior_owner_rejected(worker, binding):
    queue, evidence, db = binding
    lock, path, state = worker.claim_queue(queue, 'fixed', evidence, db)
    try:
        with pytest.raises(BlockingIOError):
            worker.claim_queue(queue, 'fixed', evidence, db)
        assert json.loads(path.read_text())==state
    finally:
        lock.close()
    with pytest.raises(ValueError,match='alive|unproven'):
        worker.claim_queue(queue, 'fixed', evidence, db)


def test_dead_waiting_worker_resumes_but_never_replays_running(worker, binding, monkeypatch):
    queue, evidence, db = binding
    lock, path, state=worker.claim_queue(queue,'fixed',evidence,db)
    lock.close()
    monkeypatch.setattr(worker,'prior_worker_dead',lambda old:True)
    lock, path, resumed=worker.claim_queue(queue,'fixed',evidence,db)
    assert len(resumed['resumes'])==1 and resumed['provider_execution_started'] is False
    resumed.update(status='running',provider_execution_started=True)
    worker._write_summary(path,resumed)
    lock.close()
    with pytest.raises(ValueError,match='never replay'):
        worker.claim_queue(queue,'fixed',evidence,db)
    assert json.loads(path.read_text())==resumed


@pytest.mark.parametrize('reported_tokens,unknown_tokens,during_refresh', [
    (0, 0, 0), (25364, 0, 0), (0, 6000, 0), (0, 0, 25364),
])
def test_exactly_one_start_with_available_shared_balance(worker,binding,monkeypatch,
                                                       reported_tokens,unknown_tokens,during_refresh):
    queue,evidence,db=binding
    ledger=FreeQuotaLedger(db)
    seed_usage(ledger,reported_tokens,reported=True)
    seed_usage(ledger,unknown_tokens,reported=False)
    lock,path,state=worker.claim_queue(queue,'fixed',evidence,db)
    starts=[]
    durable=[]
    original_fsync=os.fsync
    def fsync(fd):
        original_fsync(fd)
        durable.append((os.readlink(f'/proc/self/fd/{fd}'),
                        json.loads(path.read_text())['provider_execution_started']))
    monkeypatch.setattr(worker.os,'fsync',fsync)
    def refresh(original,out):
        seed_usage(ledger,during_refresh,reported=True)
        return evidence
    monkeypatch.setattr(worker,'refresh_evidence',refresh)
    class Child:
        pid=os.getpid()
        returncode=0
        def poll(self): return 0
    def popen(command,**kwargs):
        # A durable no-replay marker must exist BEFORE subprocess creation.
        saved=json.loads(path.read_text())
        assert saved['provider_execution_started'] is True and saved['status']=='running'
        assert (str(path),True) in durable and (str(queue),True) in durable
        assert kwargs['env']['GROQ_FREE_QUOTA_EVIDENCE']==str(evidence.resolve())
        starts.append(command)
        return Child()
    monkeypatch.setattr(worker.subprocess,'Popen',popen)
    try:
        worker.run_worker(args(queue),{},ledger,path,state)
    finally:
        lock.close()
    assert len(starts)==1
    snapshot=ledger.snapshot()
    assert snapshot['reported_tokens']==reported_tokens+during_refresh
    assert snapshot['unknown_reserved_tokens']==unknown_tokens
    assert state['admission_policy']=='available_daily_balance_per_request'
    assert 'required_headroom' not in state
    command=starts[0]
    assert command[command.index('--source-id')+1]=='copom281-20260916'
    assert command[command.index('--configurations')+1]=='literal-quality'
    assert command[command.index('--repeats')+1]=='1'
    assert command[command.index('--max-runs')+1]=='1'
    assert '--free-only' in command
    monkeypatch.setattr(worker,'prior_worker_dead',lambda old:True)
    with pytest.raises(ValueError,match='never replay'):
        worker.claim_queue(queue,'fixed',evidence,db)
    assert len(starts)==1


def test_daily_quota_exhausted_during_refresh_never_starts(worker,binding,monkeypatch):
    queue,evidence,db=binding
    ledger=FreeQuotaLedger(db)
    lock,path,state=worker.claim_queue(queue,'fixed',evidence,db)
    def refresh(*_):
        seed_usage(ledger,200000,reported=True)
        return evidence
    monkeypatch.setattr(worker,'refresh_evidence',refresh)
    try:
        worker.run_worker(args(queue),{},ledger,path,state)
    finally:
        lock.close()
    assert state['status']=='waiting_quota' and state['provider_execution_started'] is False
    assert ledger.snapshot()['reported_tokens']==200000


def test_exhausted_request_quota_waits_for_first_release(worker,binding,monkeypatch):
    queue,evidence,db=binding
    ledger=FreeQuotaLedger(db,QuotaWindow(requests_per_day=2))
    first=datetime.now(timezone.utc)-timedelta(hours=2)
    for offset in (0,1):
        ledger.reserve_attempt(model='openai/gpt-oss-20b',job_id='other-profile',
            payload_sha256='unknown',tokens=1,now=first+timedelta(hours=offset))
    lock,path,state=worker.claim_queue(queue,'fixed',evidence,db)
    monkeypatch.setattr(worker.time,'sleep',lambda *_:worker.stop())
    try:
        worker.run_worker(args(queue),{},ledger,path,state)
    finally:
        lock.close()
    assert state['status']=='stopped_before_execution' and state['provider_execution_started'] is False
    assert state['remaining_tokens']==199998
    assert datetime.fromisoformat(state['next_window_available_at'])==first+timedelta(days=1)


@pytest.mark.parametrize('exhaust_after_refresh', [False, True])
def test_zero_call_deferral_resumes_with_new_artifacts(worker,binding,monkeypatch,exhaust_after_refresh):
    from suno_asset.m7_literal_eval import configuration, execution_identity, make_execution_protocol
    queue,evidence,db=binding
    ledger=FreeQuotaLedger(db)
    fingerprint={'candidate':'deferral-test'}
    candidate=sha256(json.dumps(fingerprint,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(worker,'fingerprint_hash',lambda:candidate)
    invocation=SimpleNamespace(queue_dir=queue,fingerprint=candidate)
    protocol=make_execution_protocol('main',2,
        {'extract':.06,'verify':.05,'generate':.03,'evaluate':.06,'refine':.06})
    identity,job_id=execution_identity({'id':'copom281-20260916','sha256':'a'*64},
        'literal-quality','beginner','analytical_text',1,candidate,protocol)
    report={'status':'quota_deferred','rows':[],'fingerprint':fingerprint,
        'execution_protocol':protocol,'configuration_specs':[configuration('literal-quality')],
        'provider_calls_real':True,'quota_mode':'free_only','authorized_billing_usd':0,
        'heldout_included':False,'paid':False,'matrix':False,'max_refinements':2,
        'max_cost_usd':.15,'stage_cost_caps_usd':protocol['stage_cost_limits']}
    if not exhaust_after_refresh:
        seed_usage(ledger,199000,reported=True)
    lock,path,state=worker.claim_queue(queue,candidate,evidence,db)
    monkeypatch.setattr(worker,'refresh_evidence',lambda *_:evidence)
    starts=[]
    class Child:
        pid=os.getpid()
        returncode=0
        def poll(self):return 0
    def popen(command,**kwargs):
        out=Path(command[command.index('--out')+1])
        assert not out.exists()
        out.mkdir(parents=True)
        starts.append(out)
        if len(starts)==1:
            if exhaust_after_refresh:
                seed_usage(ledger,200000,reported=True)
            ledger.register_job(job_id,candidate,identity['source_id'],
                json.dumps(identity,ensure_ascii=False,sort_keys=True))
            ledger.start_job(job_id)
            with pytest.raises(ExpectedServiceError) as exc:
                ledger.reserve_attempt(model='openai/gpt-oss-120b',job_id=job_id,
                    payload_sha256='first-call',tokens=2000)
            assert exc.value.code=='free_quota_deferred'
            ledger.defer_job(job_id)
            worker._write_summary(out/'summary.json',report)
        else:
            worker._write_summary(out/'summary.json',{'status':'completed_pending_review','rows':[]})
        return Child()
    monkeypatch.setattr(worker.subprocess,'Popen',popen)
    try:
        worker.run_worker(invocation,{},ledger,path,state)
        assert state['status']=='waiting_quota' and state['provider_execution_started'] is False
        assert len(state['unstarted_deferrals'])==1
        proof=state['unstarted_deferrals'][0]
        first_summary=starts[0]/'summary.json'
        assert proof['summary_sha256']==sha256(first_summary.read_bytes()).hexdigest()
        assert proof['attempts']==0 and ledger.job(job_id)['status']=='queued'
        # Advancing the wait does not rewrite the first invocation's artifacts.
        # The child remains responsible for reserving its concrete request.
        state['deferred_until']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        monkeypatch.setattr(worker,'quota_state',lambda *_:(
            {'attempts_24h':0,'reported_tokens':0,'unknown_reserved_tokens':0},
            0,datetime.now(timezone.utc).isoformat()))
        worker.run_worker(invocation,{},ledger,path,state)
        assert state['status']=='finished_pending_review' and state['provider_execution_started'] is True
        assert len(starts)==2 and starts[1]!=starts[0]
        assert sha256(first_summary.read_bytes()).hexdigest()==proof['summary_sha256']
    finally:
        lock.close()


@pytest.mark.parametrize('known_usage', [False, True])
def test_deferral_claim_never_reopens_job_with_attempt(worker,binding,monkeypatch,known_usage):
    from suno_asset.m7_literal_eval import configuration, execution_identity, make_execution_protocol
    queue,evidence,db=binding
    ledger=FreeQuotaLedger(db)
    fingerprint={'candidate':'attempted-test'}
    candidate=sha256(json.dumps(fingerprint,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    protocol=make_execution_protocol('main',2,
        {'extract':.06,'verify':.05,'generate':.03,'evaluate':.06,'refine':.06})
    identity,job_id=execution_identity({'id':'copom281-20260916','sha256':'a'*64},
        'literal-quality','beginner','analytical_text',1,candidate,protocol)
    ledger.register_job(job_id,candidate,identity['source_id'],json.dumps(identity,sort_keys=True))
    ledger.start_job(job_id)
    attempt=ledger.reserve_attempt(model='openai/gpt-oss-120b',job_id=job_id,
        payload_sha256='possibly-sent',tokens=100)
    if known_usage:ledger.settle_attempt(attempt,actual_tokens=50,status='reported')
    report={'status':'quota_deferred','rows':[],'fingerprint':fingerprint,
        'execution_protocol':protocol,'configuration_specs':[configuration('literal-quality')],
        'provider_calls_real':True,'quota_mode':'free_only','authorized_billing_usd':0,
        'heldout_included':False,'paid':False,'matrix':False,'max_refinements':2,
        'max_cost_usd':.15,'stage_cost_caps_usd':protocol['stage_cost_limits']}
    summary=queue/'summary.json';queue.mkdir();worker._write_summary(summary,report)
    with pytest.raises(ValueError,match='zero provider attempts'):
        worker.unstarted_deferral(ledger,SimpleNamespace(fingerprint=candidate),report,summary,0)
    assert ledger.job(job_id)['status']=='running'
    assert ledger.snapshot()['unknown_reserved_tokens']==(0 if known_usage else 100)


def test_fingerprint_changed_during_refresh_never_starts(worker,binding,monkeypatch):
    queue,evidence,db=binding
    lock,path,state=worker.claim_queue(queue,'fixed',evidence,db)
    def refresh(*_):
        monkeypatch.setattr(worker,'fingerprint_hash',lambda:'new-version')
        return evidence
    monkeypatch.setattr(worker,'refresh_evidence',refresh)
    try:
        worker.run_worker(args(queue),{},FreeQuotaLedger(db),path,state)
    finally:
        lock.close()
    saved=json.loads(path.read_text())
    assert saved['status']=='halted' and saved['provider_execution_started'] is False


def test_pid_reuse_is_checked_with_birth_identity(worker,monkeypatch):
    monkeypatch.setattr(worker,'process_identity',lambda pid:{'boot_id':'boot','start_ticks':'100','state':'S'})
    assert not worker.prior_worker_dead({'pid':1})
    assert not worker.prior_worker_dead({'pid':1,'worker_identity':{'boot_id':'boot','start_ticks':'100'}})
    assert worker.prior_worker_dead({'pid':1,'worker_identity':{'boot_id':'boot','start_ticks':'99'}})


def test_authenticated_refresh_preserves_free_attestation_and_limits(worker,tmp_path,monkeypatch):
    # Restore the real refresh function by reloading a separate module; its only
    # HTTP operation is replaced with a local response below.
    spec=importlib.util.spec_from_file_location('m7_wait_refresh_test',worker.__file__)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    attachment=tmp_path/'operator.txt'
    attachment.write_text('Operator attests free account and unchanged limits.')
    evidence=tmp_path/'original.json'
    original={'account_plan':'free','zero_billing_confirmed':True,
              'limits':QuotaWindow().__dict__,'checked_at':'2026-10-14T00:00:00+00:00',
              'credential_sha256':sha256(b'fake-key').hexdigest(),
              'models_available':['openai/gpt-oss-120b','openai/gpt-oss-20b'],
              'evidence_file':attachment.name,'evidence_sha256':sha256(attachment.read_bytes()).hexdigest()}
    evidence.write_text(json.dumps(original))
    monkeypatch.setenv('GROQ_FREE_QUOTA_EVIDENCE',str(evidence))
    calls=[]
    monkeypatch.setattr(module,'make_session',lambda **kw:SimpleNamespace(api_key='fake-key'))
    response=SimpleNamespace(status_code=200,content=b'local catalogue',
        json=lambda:{'data':[{'id':name,'active':True} for name in original['models_available']]})
    def get(url,**kwargs):
        calls.append(url)
        return response
    monkeypatch.setattr(module.httpx,'get',get)
    out=tmp_path/'refresh';out.mkdir()
    renewed=json.loads(module.refresh_evidence(original,out).read_text())
    assert calls==['https://api.groq.com/openai/v1/models']
    assert renewed['account_plan']=='free' and renewed['zero_billing_confirmed'] is True
    assert renewed['limits']==original['limits']
    assert renewed['evidence_sha256']==original['evidence_sha256']
    assert json.loads(evidence.read_text())==original
    receipt=json.loads((out/'authenticated-availability.json').read_text())
    assert receipt['billing_plan_independently_verified'] is False
