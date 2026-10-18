"""Gemini provider contracts exercised offline, with no real key or inference."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
import json
from urllib.error import HTTPError, URLError
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from suno_asset.api import app as api_module
from suno_asset.services import gemini
from suno_asset.services.gemini_quota import GeminiQuotaLedger, GeminiQuotaWindow
from suno_asset.services.groq import STAGE_OUTPUT, GroqSession
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.observability import attach_provider_audit
from suno_asset.services.provider_config import environment_value, make_economy_session, provider_identity
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.quota_ledger import FreeQuotaLedger

SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
    'required': ['ok'], 'additionalProperties': False}
REQUEST = ('extract', 'Retorne JSON.', {}, SCHEMA, 100)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ('SUNO_ECONOMY_PROVIDER', 'GEMINI_API_KEY', 'GEMINI_MODEL', 'GEMINI_FREE_ONLY',
        'GEMINI_TPM_LIMIT', 'GEMINI_RPM_LIMIT', 'GEMINI_RPD_LIMIT', 'GEMINI_TPD_LIMIT',
        'GEMINI_QUOTA_SCOPE', 'GEMINI_FREE_QUOTA_DIR'):
        monkeypatch.delenv(name, raising=False)


def response(content=None, *, usage=True, finish='stop'):
    raw = {'choices': [{'finish_reason': finish, 'message': {
        'content': json.dumps({'ok': True} if content is None else content)}}]}
    if usage is True:
        raw['usage'] = {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30,
            'completion_tokens_details': {'reasoning_tokens': 4}, 'prompt_tokens_details': {'cached_tokens': 7}}
    elif usage is not False:
        raw['usage'] = usage
    return raw


def session(tmp_path, *, transport=None, **kwargs):
    return gemini.GeminiSession('offline-secret', transport=transport or (lambda *_: response()),
        free_only=True, **kwargs)


def http_error(status, *, headers=None, error=None):
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = str(value)
    return HTTPError(gemini.ENDPOINT, status, 'provider error', message,
        BytesIO(json.dumps({'error': error or {}}).encode()))


def test_configuration_reads_environment_then_local_dotenv_without_exposing_key(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('export GEMINI_API_KEY="local-secret" # local only\n'
        'GEMINI_FREE_ONLY=1\nGEMINI_MODEL=gemini-3.8-flash\nSUNO_ECONOMY_PROVIDER=gemini\nGEMINI_RPM_LIMIT=7\n')
    s = make_economy_session()
    assert isinstance(s, gemini.GeminiSession) and s.api_key == 'local-secret'
    assert s.rpm_limit == 7 and s.tpm_limit is None and s.tpd_limit is None
    assert 'local-secret' not in json.dumps(s.manifest())
    monkeypatch.setenv('GEMINI_API_KEY', 'env-secret')
    assert gemini.make_session().api_key == 'env-secret'
    monkeypatch.setenv('GEMINI_API_KEY', '')
    with pytest.raises(ExpectedServiceError, match='GEMINI_API_KEY') as exc:
        gemini.make_session()
    assert exc.value.code == 'gemini_key_missing'
    assert environment_value('GEMINI_API_KEY') == ''


def test_provider_selection_keeps_historical_groq_profiles(monkeypatch):
    monkeypatch.setenv('GROQ_MODEL', 'openai/gpt-oss-20b')
    assert provider_identity('single')['provider'] == 'groq'
    assert set(provider_identity('quality')['models'].values()) == {'openai/gpt-oss-120b'}
    assert provider_identity('hybrid')['models']['extract'] == 'openai/gpt-oss-20b'
    assert provider_identity('economy')['provider'] == 'gemini'
    monkeypatch.setenv('SUNO_ECONOMY_PROVIDER', 'groq')
    assert provider_identity('economy')['models']['generate'] == 'openai/gpt-oss-20b'
    monkeypatch.setenv('SUNO_ECONOMY_PROVIDER', 'invalid')
    with pytest.raises(ExpectedServiceError) as exc:
        provider_identity('economy')
    assert exc.value.code == 'provider_config_invalid'


@pytest.mark.parametrize('name,value,code', [('GEMINI_MODEL', 'unknown-model', 'llm_price_unknown'),
    ('GEMINI_RPM_LIMIT', '-1', 'gemini_config_invalid'), ('GEMINI_TPM_LIMIT', 'abc', 'gemini_config_invalid')])
def test_invalid_config_fails_before_transport(monkeypatch, name, value, code):
    monkeypatch.setenv(name, value)
    with pytest.raises(ExpectedServiceError) as exc:
        provider_identity('economy')
    assert exc.value.code == code


def test_all_stage_payloads_and_repair_stay_on_configured_gemini(tmp_path):
    s = session(tmp_path)
    for stage in STAGE_OUTPUT:
        payload = s.payload(stage, 'instrução', {'text': 'fonte'}, SCHEMA, 100)
        assert payload['model'] == gemini.MODEL and payload['reasoning_effort'] == 'low'
        assert payload['max_tokens'] == 100 and 'max_completion_tokens' not in payload
        assert set(payload) == {'model', 'messages', 'reasoning_effort', 'max_tokens', 'temperature', 'response_format'}
        assert payload['response_format']['json_schema']['schema'] == SCHEMA
        assert s.repair_model_for(stage) == gemini.MODEL
        assert s.call(stage, 'instrução', {}, SCHEMA, 100) == {'ok': True}
    s.model_overrides['extract'] = 'openai/gpt-oss-120b'
    with pytest.raises(ExpectedServiceError) as exc:
        s.call(*REQUEST)
    assert exc.value.code == 'gemini_model_unsupported' and s.calls == 5
    s.model_overrides.pop('extract')
    assert set(s.manifest()['models_used']['verify']) == {gemini.MODEL}


@pytest.mark.parametrize(('model','expect_reasoning'), [
    ('gemini-3.6-flash', 'low'),
    ('gemini-3.5-flash-lite', None),
])
def test_lower_cost_gemini_models_use_supported_generation_parameters(tmp_path, model, expect_reasoning):
    s = session(tmp_path, model=model)
    payload = s.payload('extract', 'instrução', {'text':'fonte'}, SCHEMA, 100)
    assert payload['model'] == model
    assert 'temperature' not in payload
    assert payload.get('reasoning_effort') == expect_reasoning


@pytest.mark.parametrize(('model','expect_reasoning'), [
    ('gemini-3.6-flash', 'low'),
    ('gemini-3.5-flash-lite', 'minimal'),
])
def test_configuration_records_effective_model_reasoning_effort(monkeypatch, model, expect_reasoning):
    monkeypatch.setenv('GEMINI_MODEL', model)
    assert gemini.configuration()['reasoning_effort'] == expect_reasoning


def test_shared_http_transport_uses_gemini_endpoint_and_preserves_raw_response(tmp_path, monkeypatch):
    received = []
    def post(url, *, json, headers, timeout):
        received.append((url, json, headers, timeout))
        return httpx.Response(200, json=response(), request=httpx.Request('POST', url))
    monkeypatch.setattr('suno_asset.services.groq.httpx.post', post)
    s = gemini.GeminiSession('offline-secret', free_only=True)
    assert s.call(*REQUEST) == {'ok': True}
    assert received[0][0] == gemini.ENDPOINT
    assert received[0][2]['Authorization'] == 'Bearer offline-secret'
    assert 25 < received[0][3] <= 300
    assert s.attempt_records[0]['usage']['raw_usage'] == response()['usage']


@pytest.mark.parametrize('deadline', [10, 120, 2400])
def test_transport_timeout_stays_within_execution_deadline(tmp_path, deadline):
    received = []
    def transport(payload, key, timeout):
        received.append(timeout)
        return response()
    s = session(tmp_path, transport=transport, deadline_seconds=deadline)
    s.call(*REQUEST)
    assert 0 < received[0] <= min(deadline, 300)
    assert received[0] > min(deadline, 300) - 1
    assert s.attempt_records[0]['transport_timeout_seconds'] == received[0]


def test_usage_reasoning_and_cache_do_not_double_count(tmp_path):
    s = session(tmp_path)
    s.preflight('extract', [REQUEST[1:]])
    s.call(*REQUEST)
    usage = s.attempt_records[0]['usage']
    assert usage['total_tokens'] == 30 and usage['reasoning_tokens'] == 4
    assert usage['visible_output_tokens'] == 6 and usage['cached_tokens'] == 7
    assert s.reserved_tokens == 30 and s.completion_tokens == 10
    assert s.quota_ledger.snapshot()['reported_tokens'] == 30
    assert s.manifest()['provider_limits']['configured']['tokens_per_minute'] is None
    assert s.plans[0]['budgets'][0]['tpm_limit'] is None
    assert s.plans[0]['budgets'][0]['planning_token_ceiling']==s.context_limit
    assert s.manifest()['account_plan_verified'] is False
    assert s.manifest()['authorized_billing_usd'] == 0


@pytest.mark.parametrize('usage', [False, {}, None, {'prompt_tokens': True, 'completion_tokens': 0},
    {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 34},
    {'prompt_tokens': 20, 'completion_tokens': 10, 'completion_tokens_details': {'reasoning_tokens': 14}}])
def test_unknown_or_inconsistent_usage_retains_reservation(tmp_path, usage):
    s = session(tmp_path, transport=lambda *_: response(usage=usage))
    reserve = s.budget(*REQUEST).total
    s.call(*REQUEST)
    assert s.reserved_tokens == reserve
    assert s.attempt_records[0]['prompt_tokens'] is None
    assert s.attempt_records[0]['usage_known'] is False
    assert s.quota_ledger.snapshot()['unknown_reserved_tokens'] == reserve
    assert not s.records


@pytest.mark.parametrize('content', [{'ok': 'true'}, {'ok': True, 'extra': 1}, {}, [], None])
def test_invalid_schema_does_not_retry_and_settles_reported_usage(tmp_path, content):
    s = session(tmp_path, transport=lambda *_: response(content) if content is not None else {
        **response(), 'choices': []})
    with pytest.raises(ExpectedServiceError) as exc:
        s.call(*REQUEST)
    assert exc.value.code == 'invalid_llm_response'
    assert s.calls == 1 and s.quota_ledger.snapshot()['reported_tokens'] == 30


@pytest.mark.parametrize('error,code', [(URLError('DNS failed'), 'gemini_unavailable'),
    (TimeoutError(), 'gemini_unavailable'), (ValueError(), 'invalid_llm_response'),
    (http_error(401), 'gemini_auth'), (http_error(403), 'gemini_auth'),
    (http_error(404), 'gemini_model_unavailable'), (http_error(400), 'gemini_request_invalid'),
    (http_error(400, error={'details': [{'reason': 'API_KEY_INVALID'}]}), 'gemini_auth'),
    (http_error(400, error={'message': 'Invalid response schema'}), 'gemini_schema_invalid'),
    (http_error(503, error={'status': 'UNAVAILABLE'}), 'gemini_unavailable'),
    (http_error(500), 'gemini_http_error')])
def test_transport_failures_are_distinct_and_uncertain_calls_never_retry(tmp_path, error, code):
    def fail(*_):
        raise error
    s = session(tmp_path, transport=fail)
    with pytest.raises(ExpectedServiceError) as exc:
        s.call(*REQUEST)
    assert exc.value.code == code and s.calls == 1
    assert s.quota_ledger.snapshot()['unknown_reserved_tokens'] == s.reserved_tokens > 0


@pytest.mark.parametrize('headers,details,delay', [({'Retry-After': '60'}, [], 60),
    ({}, [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '120s'}], 120),
    ({}, [], None)])
def test_429_persists_account_cooldown_across_sessions_without_retry(tmp_path, headers, details, delay):
    def fail(*_):
        raise http_error(429, headers=headers, error={'details': details})
    s = session(tmp_path, transport=fail)
    with pytest.raises(ExpectedServiceError) as exc:
        s.call(*REQUEST)
    assert exc.value.code == 'gemini_rate_limit' and s.calls == 1
    assert s.attempt_records[0]['retry_after_seconds'] == delay
    next_session = session(tmp_path, transport=lambda *_: pytest.fail('must respect persisted 429'))
    with pytest.raises(ExpectedServiceError) as exc:
        next_session.call(*REQUEST)
    assert exc.value.code == 'gemini_rate_limit' and next_session.calls == 0
    cooldown = next_session.quota_ledger.snapshot()['cooldown']
    assert (cooldown['until_at'] is None) == (delay is None)


def test_gemini_ledger_is_separate_from_groq_and_accounts_for_unknown_limits(tmp_path):
    groq = FreeQuotaLedger(tmp_path / 'groq.sqlite3')
    groq.reserve_attempt(model='openai/gpt-oss-20b', job_id='old', payload_sha256='old', tokens=7000)
    s = session(tmp_path)
    assert s.quota_ledger.path != groq.path
    assert s.quota_ledger.snapshot()['attempts_24h'] == 0
    s.call(*REQUEST)
    assert groq.snapshot()['attempts_24h'] == 1
    assert s.quota_ledger.snapshot()['attempts_24h'] == 1
    with pytest.raises(ExpectedServiceError):
        gemini.GeminiSession('fake', quota_ledger=groq)


def test_configured_quota_and_attempt_token_cost_limits_apply_before_transport(tmp_path):
    s = session(tmp_path, limits=GeminiQuotaWindow(requests_per_day=1))
    s.call(*REQUEST)
    with pytest.raises(ExpectedServiceError) as exc:
        s.call(*REQUEST)
    assert exc.value.code == 'free_quota_deferred' and s.calls == 1
    for options, code in [({'max_calls': 0}, 'llm_budget_exceeded'),
        ({'max_tokens': 1}, 'llm_budget_exceeded'), ({'deadline_seconds': 0}, 'llm_deadline'),
        ({'nominal_execution_cap_usd': 0}, 'llm_execution_nominal_cap'),
        ({'stage_cost_limits': {'extract': 0}}, 'llm_stage_cost_budget'),
        ({'limits': GeminiQuotaWindow(tokens_per_minute=50)}, 'llm_request_budget')]:
        blocked = session(tmp_path, transport=lambda *_: pytest.fail('budget must block'), **options)
        with pytest.raises(ExpectedServiceError) as exc:
            blocked.call(*REQUEST)
        assert exc.value.code == code and blocked.calls == 0


def test_free_only_declaration_is_required_even_with_valid_key(tmp_path):
    s = gemini.GeminiSession('offline-secret', transport=lambda *_: pytest.fail('must require free declaration'))
    with pytest.raises(ExpectedServiceError) as exc:
        s.call(*REQUEST)
    assert exc.value.code == 'free_quota_required' and s.calls == 0


def test_audit_saves_raw_usage_but_never_key_and_computes_nominal_cost(tmp_path):
    store = LocalStorage(tmp_path / 'store')
    source = store.put_text('Fonte pública.')
    run, _ = store.repository.claim(str(source.source_id), 'fingerprint', 'real')
    run_id = run['request_id']
    s = session(tmp_path)
    attach_provider_audit(store, s, UUID(run_id))
    s.call(*REQUEST)
    saved = list((store.root / 'runs').rglob('*'))
    files = [p.read_text() for p in saved if p.is_file()]
    assert all('offline-secret' not in value for value in files)
    events = store.repository.get_events(run_id)
    event = next(row for row in events if row['node'] == 'provider' and row['status'] == 'received')
    assert event['metrics']['model'] == gemini.MODEL
    assert event['metrics']['provider'] == 'gemini'
    assert event['metrics']['nominal_cost_usd'] == (20 * .75 + 10 * 3.75) / 1e6


def test_api_exposes_ready_hybrid_configuration(tmp_path):
    client = TestClient(api_module.create_app(storage=LocalStorage(tmp_path / 'store')))
    caps = client.get('/capabilities').json()
    assert caps['economy_configuration']['provider'] == 'gemini'
    assert caps['economy_preparation_ready'] is True
    assert caps['economy_policy_version'] == 'mvp-hybrid-facts-v2'
    assert caps['economy_call_targets'] == {'fresh':4,'approved_fact_cache':2,'idempotent_replay':0}


def test_manifest_uses_resolved_configuration_and_key_is_not_in_repr(tmp_path, monkeypatch):
    s = session(tmp_path)
    monkeypatch.setenv('GEMINI_MODEL', 'changed-after-construction')
    assert s.manifest()['models']['generate'] == gemini.MODEL
    assert 'offline-secret' not in repr(s)


def test_provider_identity_changes_fact_cache_key(monkeypatch):
    from suno_asset.experiments.literal_efficiency.cache import cache_key
    baseline = dict.fromkeys(('source_sha256', 'block_id', 'context_sha256', 'parser_version',
        'segmentation_version', 'extractor_version', 'model_config', 'prompt_version',
        'schema_version', 'verification_policy'), 'fixed')
    first = cache_key({**baseline, 'provider_configuration': provider_identity('economy')})
    monkeypatch.setenv('SUNO_ECONOMY_PROVIDER', 'groq')
    assert cache_key({**baseline, 'provider_configuration': provider_identity('economy')}) != first


def test_smoke_is_separate_and_requires_explicit_execution(tmp_path, monkeypatch):
    from suno_asset import gemini_smoke
    s = session(tmp_path, transport=lambda *_: response({'status': 'ok'}))
    monkeypatch.setattr(gemini_smoke, 'make_session', lambda: s)
    out = tmp_path / 'probe'
    assert gemini_smoke.probe(out)['calls'] == 0 and not out.exists()
    result = gemini_smoke.probe(out, execute=True)
    assert result['status'] == 'completed' and result['calls'] == 1
    assert result['reported_tokens'] == 30 and result['usage_complete'] is True
    assert result['separate_from_pipeline_budget'] is True and s.max_calls == 1
    assert any((out / 'call-audit').glob('*-received.json'))
    assert 'offline-secret' not in (out / 'result.json').read_text()
    monkeypatch.setattr(gemini_smoke, 'make_session', lambda: session(tmp_path))
    with pytest.raises(FileExistsError):
        gemini_smoke.probe(out, execute=True)
    assert s.calls == 1


def test_observed_provider_balance_only_restricts_admission(tmp_path):
    raw = response()
    raw['_rate_limits'] = {'remaining': '0', 'reset': '60s'}
    s = session(tmp_path, transport=lambda *_: raw)
    s.call(*REQUEST)
    next_session = session(tmp_path, transport=lambda *_: pytest.fail('observed quota must block'))
    with pytest.raises(ExpectedServiceError) as exc:
        next_session.call(*REQUEST)
    assert exc.value.code == 'gemini_rate_limit' and next_session.calls == 0


@pytest.mark.parametrize('finish,refusal,code', [('length', None, 'llm_truncated'),
    ('content_filter', 'blocked', 'llm_refusal')])
def test_partial_or_refused_response_is_never_retried(tmp_path, finish, refusal, code):
    raw = response(finish=finish)
    raw['choices'][0]['message']['refusal'] = refusal
    s = session(tmp_path, transport=lambda *_: raw)
    with pytest.raises(ExpectedServiceError) as exc:
        s.call(*REQUEST)
    assert exc.value.code == code and s.calls == 1
    assert s.quota_ledger.snapshot()['reported_tokens'] == 30


@pytest.mark.parametrize('content', ['{"ok": false, "ok": true}', '{"ok": NaN}', 'broken JSON'])
def test_malformed_or_ambiguous_json_is_not_accepted(tmp_path, content):
    raw = response()
    raw['choices'][0]['message']['content'] = content
    s = session(tmp_path, transport=lambda *_: raw)
    with pytest.raises(ExpectedServiceError) as exc:
        s.call(*REQUEST)
    assert exc.value.code == 'invalid_llm_response' and s.calls == 1
