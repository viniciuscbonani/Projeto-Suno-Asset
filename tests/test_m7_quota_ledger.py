from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import pytest

from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.groq import GroqSession
from suno_asset.services.quota_ledger import FreeQuotaLedger, QuotaWindow


def test_unknown_usage_retains_full_reservation_across_connections(tmp_path):
    path = tmp_path / 'quota.sqlite3'
    first = FreeQuotaLedger(path, QuotaWindow(30, 1000, 8000, 200000))
    second = FreeQuotaLedger(path, QuotaWindow(30, 1000, 8000, 200000))
    attempt = first.reserve_attempt(model='openai/gpt-oss-20b', job_id='job-a',
        payload_sha256='a' * 64, tokens=5000)
    first.settle_attempt(attempt, actual_tokens=None, status='unknown', http_status=504)

    with pytest.raises(ExpectedServiceError, match='TPM'):
        second.reserve_attempt(model='openai/gpt-oss-120b', job_id='job-b',
            payload_sha256='b' * 64, tokens=3001)

    snapshot = second.snapshot()
    assert snapshot['unknown_reserved_tokens'] == 5000
    assert snapshot['reported_tokens'] == 0


def test_reported_usage_releases_token_reservation_but_not_request_count(tmp_path):
    ledger = FreeQuotaLedger(tmp_path / 'quota.sqlite3', QuotaWindow(30, 1000, 8000, 200000))
    attempt = ledger.reserve_attempt(model='m', job_id='job', payload_sha256='a' * 64, tokens=7000)
    ledger.settle_attempt(attempt, actual_tokens=1200, status='reported', http_status=200)

    following = ledger.reserve_attempt(model='m', job_id='job', payload_sha256='b' * 64, tokens=6800)
    assert following
    assert ledger.snapshot()['reported_tokens'] == 1200


def test_local_tpd_gate_can_be_disabled_without_disabling_tpm(tmp_path):
    ledger=FreeQuotaLedger(tmp_path/'partial-tpd.sqlite3',QuotaWindow(30,1000,8000,100),
        enforce_tokens_per_day=False)
    start=datetime(2026,10,13,12,0,tzinfo=timezone.utc)
    first=ledger.reserve_attempt(model='m',job_id='job',payload_sha256='a'*64,tokens=80,now=start)
    ledger.settle_attempt(first,actual_tokens=80,status='reported')
    second=ledger.reserve_attempt(model='m',job_id='job',payload_sha256='b'*64,tokens=80,
        now=start+timedelta(seconds=61))
    ledger.settle_attempt(second,actual_tokens=80,status='reported')
    assert second and ledger.snapshot(now=start+timedelta(seconds=61))['reported_tokens']==160
    with pytest.raises(ExpectedServiceError,match='pedido individual'):
        ledger.reserve_attempt(model='m',job_id='job',payload_sha256='c'*64,tokens=8001,
            now=start+timedelta(seconds=122))


def test_free_session_exposes_operator_disabled_local_tpd_gate(monkeypatch,tmp_path):
    monkeypatch.setenv('GROQ_FREE_DISABLE_LOCAL_TPD','1')
    ledger=FreeQuotaLedger(tmp_path/'partial-tpd.sqlite3')
    session=GroqSession('test-key',free_only=True,quota_ledger=ledger)
    assert not ledger.enforce_tokens_per_day
    assert session.manifest()['local_tpd_admission_enforced'] is False


def test_quota_windows_expire_only_after_their_rolling_period(tmp_path):
    ledger = FreeQuotaLedger(tmp_path / 'quota.sqlite3', QuotaWindow(1, 1000, 8000, 200000))
    start = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    attempt = ledger.reserve_attempt(model='m', job_id='job', payload_sha256='a' * 64,
        tokens=100, now=start)
    ledger.settle_attempt(attempt, actual_tokens=100, status='reported', http_status=200)

    with pytest.raises(ExpectedServiceError, match='2026-10-13T12:01:00'):
        ledger.reserve_attempt(model='m', job_id='job', payload_sha256='b' * 64,
            tokens=100, now=start + timedelta(seconds=59))
    assert ledger.reserve_attempt(model='m', job_id='job', payload_sha256='c' * 64,
        tokens=100, now=start + timedelta(seconds=61))


def test_daily_window_returns_next_availability_without_sleeping(tmp_path):
    ledger=FreeQuotaLedger(tmp_path/'quota.sqlite3',QuotaWindow(30,1,8000,200000))
    start=datetime(2026,10,13,12,0,tzinfo=timezone.utc)
    attempt=ledger.reserve_attempt(model='m',job_id='job',payload_sha256='a'*64,tokens=10,now=start)
    ledger.settle_attempt(attempt,actual_tokens=10,status='reported',http_status=200)
    with pytest.raises(ExpectedServiceError,match='2026-10-14T12:00:00'):
        ledger.reserve_attempt(model='m',job_id='job',payload_sha256='b'*64,tokens=10,now=start+timedelta(minutes=1))
    deferred=ledger.next_available_window('job')
    assert deferred['available_at']=='2026-10-14T12:00:00+00:00'
    assert deferred['reason']=='RPD'


def test_zero_call_quota_deferral_requeues_same_job_for_later_window(tmp_path):
    ledger=FreeQuotaLedger(tmp_path/'quota.sqlite3',QuotaWindow(30,1,8000,200000))
    ledger.register_job('same-combination','fp','source','config')
    ledger.start_job('same-combination')
    start=datetime(2026,10,13,12,0,tzinfo=timezone.utc)
    attempt=ledger.reserve_attempt(model='m',job_id='prior-combination',payload_sha256='a'*64,tokens=100,now=start)
    ledger.settle_attempt(attempt,actual_tokens=100,status='reported',http_status=200)
    with pytest.raises(ExpectedServiceError):
        ledger.reserve_attempt(model='m',job_id='same-combination',payload_sha256='b'*64,tokens=100,
            now=start+timedelta(seconds=61))
    ledger.defer_job('same-combination')
    assert ledger.job('same-combination')['status']=='queued'
    assert ledger.next_available_window('same-combination')['reason']=='RPD'
    ledger.start_job('same-combination')
    assert ledger.job('same-combination')['status']=='running'


@pytest.mark.parametrize('settled', [False, True])
def test_deferral_cannot_requeue_a_job_that_already_called_provider(tmp_path, settled):
    ledger=FreeQuotaLedger(tmp_path/'quota.sqlite3',QuotaWindow(30,1,8000,200000))
    ledger.register_job('started','fp','source','config')
    ledger.start_job('started')
    start=datetime(2026,10,13,12,0,tzinfo=timezone.utc)
    attempt=ledger.reserve_attempt(model='m',job_id='started',payload_sha256='a'*64,tokens=100,now=start)
    if settled:
        ledger.settle_attempt(attempt,actual_tokens=100,status='reported',http_status=200)
    with pytest.raises(ExpectedServiceError):
        ledger.reserve_attempt(model='m',job_id='started',payload_sha256='b'*64,tokens=100,
            now=start+timedelta(seconds=61))
    with pytest.raises(ValueError,match='chamada iniciada'):
        ledger.defer_job('started')
    assert ledger.job('started')['status']=='running'


def test_run_case_zero_call_deferral_writes_no_result_and_keeps_job_resumable(tmp_path):
    from hashlib import sha256
    from io import BytesIO
    from reportlab.pdfgen import canvas
    from suno_asset.m7_literal_eval import run_case, make_execution_protocol, execution_identity

    buffer=BytesIO()
    pdf=canvas.Canvas(buffer)
    pdf.drawString(40,780,'Fonte sintetica para verificar adiamento por cota.')
    pdf.save()
    raw=buffer.getvalue()
    path=tmp_path/'synthetic-source.pdf'
    path.write_bytes(raw)
    source={'id':'synthetic-quota','split':'development','local_pdf':str(path),
        'sha256':sha256(raw).hexdigest(),'physical_pages':1}
    ledger=FreeQuotaLedger(tmp_path/'quota.sqlite3',QuotaWindow(30,1000,8000,200000))
    started=datetime.now(timezone.utc)-timedelta(minutes=25)
    for index in range(25):
        prior=ledger.reserve_attempt(model='openai/gpt-oss-20b',job_id='prior',
            payload_sha256=f'{index:064x}',tokens=8000,
            now=started+timedelta(seconds=index*61))
        ledger.settle_attempt(prior,actual_tokens=8000,status='reported',http_status=200)
    session=GroqSession('offline-key',transport=lambda *_:pytest.fail('cota foi adiada antes do transporte'),
        free_only=True,quota_ledger=ledger)
    session.require_free_quota_evidence=lambda:{'evidence_sha256':'test'}

    caps={stage:.06 for stage in ('extract','verify','generate','evaluate','refine')}
    protocol=make_execution_protocol('main',2,caps)
    _,job_id=execution_identity(source,'literal-quality','beginner','analytical_text',1,'f'*64,protocol)
    row=run_case(source,'literal-quality',tmp_path/'out',session=session,free_only=True,
        quota_job_id=job_id,fingerprint_sha256='f'*64,execution_protocol=protocol,stage_cost_limits=caps)

    assert row['status']=='quota_deferred'
    assert row['quota_status']=='deferred'
    assert row['attempts']==0
    assert ledger.job(job_id)['status']=='queued'
    assert ledger.next_available_window(job_id) is not None
    assert not list((tmp_path/'out').glob('synthetic-quota-literal-quality*.json'))


def test_attempt_larger_than_a_window_is_not_queued_forever(tmp_path):
    ledger=FreeQuotaLedger(tmp_path/'quota.sqlite3',QuotaWindow(30,1000,8000,200000))
    with pytest.raises(ExpectedServiceError,match='excede a menor cota'):
        ledger.reserve_attempt(model='m',job_id='job',payload_sha256='a'*64,tokens=8001)


def test_job_identity_cannot_be_rebound_to_another_fingerprint(tmp_path):
    ledger = FreeQuotaLedger(tmp_path / 'quota.sqlite3')
    ledger.register_job('matrix-281-quality', 'fingerprint-a', 'copom281', 'literal-quality')
    ledger.register_job('matrix-281-quality', 'fingerprint-a', 'copom281', 'literal-quality')

    with pytest.raises(ValueError, match='outro fingerprint'):
        ledger.register_job('matrix-281-quality', 'fingerprint-b', 'copom281', 'literal-quality')

    assert ledger.pending_jobs('fingerprint-a')[0]['status'] == 'queued'


def test_job_result_is_hash_checked_and_started_jobs_cannot_be_replayed(tmp_path):
    ledger=FreeQuotaLedger(tmp_path/'quota.sqlite3')
    ledger.register_job('job-a','fingerprint-a','copom281','literal-quality|beginner|analytical_text|1')
    ledger.start_job('job-a')
    result=tmp_path/'job-result.json';result.write_text('{"status":"complete"}')
    digest=sha256(result.read_bytes()).hexdigest()
    ledger.complete_job('job-a',result,digest)

    with pytest.raises(ValueError,match='já iniciada/concluída'):
        ledger.start_job('job-a')
    assert ledger.job('job-a')['result_sha256']==digest
    assert ledger.pending_jobs('fingerprint-a')==[]

    ledger.register_job('job-b','fingerprint-a','copom281','literal-quality|advanced|analytical_text|1')
    ledger.start_job('job-b')
    with pytest.raises(ValueError,match='hash divergente'):
        ledger.complete_job('job-b',result,'0'*64)


def test_free_only_mode_caps_each_limit_and_shares_caps_with_ledger(tmp_path):
    ledger = FreeQuotaLedger(tmp_path / 'quota.sqlite3', QuotaWindow(60, 2000, 16000, 400000))
    session = GroqSession('offline-key', transport=lambda *_: {}, free_only=True,
        quota_ledger=ledger, rpm_limit=60, rpd_limit=2000, tpm_limit=16000, tpd_limit=400000)

    assert (session.rpm_limit, session.rpd_limit, session.tpm_limit, session.tpd_limit) == (30, 1000, 8000, 200000)
    assert ledger.window == QuotaWindow(30, 1000, 8000, 200000)


def test_free_quota_calls_require_recent_account_level_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('GROQ_FREE_QUOTA_EVIDENCE', str(tmp_path / 'missing.json'))
    session = GroqSession('offline-key', transport=lambda *_: pytest.fail('transport must not run'), free_only=True,
        quota_ledger=FreeQuotaLedger(tmp_path / 'quota.sqlite3'))
    with pytest.raises(ExpectedServiceError, match='evidência atual'):
        session.require_free_quota_evidence()


def test_real_transport_refuses_operator_managed_paid_mode(monkeypatch):
    from suno_asset.services.groq import EXTRACT_SCHEMA
    monkeypatch.delenv('GROQ_FREE_ONLY',raising=False)
    session=GroqSession('offline-key')
    with pytest.raises(ExpectedServiceError) as exc:
        session.call('extract','system',{},EXTRACT_SCHEMA)
    assert exc.value.code=='free_quota_required'


def test_free_quota_evidence_binds_current_attachment_and_lower_limits(tmp_path, monkeypatch):
    attachment = tmp_path / 'console-evidence.png'
    attachment.write_bytes(b'account shows free tier and limits')
    evidence = {'version':'m7-groq-free-quota-evidence-v1','provider':'groq','account_plan':'free',
        'zero_billing_confirmed':True,'account_id_sha256':'a'*64,'checked_at':datetime.now(timezone.utc).isoformat(),
        'models_available':['openai/gpt-oss-20b','openai/gpt-oss-120b'],
        'limits':{'requests_per_minute':20,'requests_per_day':800,'tokens_per_minute':7000,'tokens_per_day':150000},
        'evidence_file':attachment.name,'evidence_sha256':sha256(attachment.read_bytes()).hexdigest()}
    path=tmp_path/'evidence.json';path.write_text(json.dumps(evidence))
    monkeypatch.setenv('GROQ_FREE_QUOTA_EVIDENCE',str(path))
    session=GroqSession('offline-key',transport=lambda *_:None,free_only=True,
        quota_ledger=FreeQuotaLedger(tmp_path/'quota.sqlite3'))

    validated=session.require_free_quota_evidence()

    assert validated['limits']['tokens_per_day']==150000
    assert validated['attachment_sha256']==evidence['evidence_sha256']
    assert session.tpd_limit==session.quota_ledger.window.tokens_per_day==150000


def test_user_attested_free_quota_evidence_can_bind_to_current_credential(tmp_path, monkeypatch):
    key='gsk-test-current-credential'
    attachment=tmp_path/'attestation.txt'
    attachment.write_text('User confirmed free plan and provided the current model limits.')
    evidence={'version':'m7-groq-free-quota-evidence-v1','provider':'groq','account_plan':'free',
        'zero_billing_confirmed':True,'credential_sha256':sha256(key.encode()).hexdigest(),
        'evidence_source':'user_attested_console_capture','checked_at':datetime.now(timezone.utc).isoformat(),
        'models_available':['openai/gpt-oss-20b','openai/gpt-oss-120b'],
        'limits':{'requests_per_minute':30,'requests_per_day':1000,'tokens_per_minute':8000,'tokens_per_day':200000},
        'evidence_file':attachment.name,'evidence_sha256':sha256(attachment.read_bytes()).hexdigest()}
    path=tmp_path/'evidence.json';path.write_text(json.dumps(evidence))
    monkeypatch.setenv('GROQ_FREE_QUOTA_EVIDENCE',str(path))
    session=GroqSession(key,transport=lambda *_:None,free_only=True,
        quota_ledger=FreeQuotaLedger(tmp_path/'quota.sqlite3'))

    validated=session.require_free_quota_evidence()

    assert validated['binding']=='credential_sha256'
    assert validated['limits']['tokens_per_day']==200000

    with pytest.raises(ExpectedServiceError,match='plano, modelos'):
        from suno_asset.services.quota_ledger import validate_free_quota_evidence
        validate_free_quota_evidence(path,{'openai/gpt-oss-20b','openai/gpt-oss-120b'},
            QuotaWindow(),credential_sha256=sha256(b'different-key').hexdigest())

