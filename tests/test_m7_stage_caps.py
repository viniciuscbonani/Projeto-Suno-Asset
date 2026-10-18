"""Nominal stage caps govern every fake-provider attempt, including retries."""
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

from suno_asset.m7_eval import PRICES
from suno_asset.services.groq import GroqSession, EXTRACT_SCHEMA
from suno_asset.services.llm_budget import RateGate
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.quota_ledger import FreeQuotaLedger


def failure(status, code):
    return HTTPError('https://synthetic.invalid',status,'synthetic provider error',{'Retry-After':'0'},
        BytesIO(json.dumps({'error':{'code':code,'message':'Synthetic retry test'}}).encode()))


def response():
    return {'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],
        'usage':{'prompt_tokens':10,'completion_tokens':5}}


def session(tmp_path, transport, model='openai/gpt-oss-120b'):
    return GroqSession('synthetic-key',model=model,transport=transport,free_only=True,
        quota_evidence={'synthetic_fixture':True},quota_job_id='synthetic-stage-cap',
        quota_ledger=FreeQuotaLedger(tmp_path/'synthetic-quota.sqlite3'))


@pytest.mark.parametrize('model',['openai/gpt-oss-20b','openai/gpt-oss-120b'])
def test_second_retry_cannot_exceed_preflight_cap_and_unknown_usage_stays_reserved(tmp_path,model):
    seen=[]
    def transport(*args):
        seen.append(args[0])
        if len(seen)==1:raise failure(400,'json_validate_failed')
        if len(seen)==2:raise failure(429,'rate_limit_exceeded')
        pytest.fail('The third attempt must be blocked before transport')
    s=session(tmp_path,transport,model)
    request=('system',{'synthetic':'source'},EXTRACT_SCHEMA,100)
    budget=s.budget('extract',*request)
    s.stage_cost_limits={'extract':2*budget.total*max(PRICES[model])/1e6+1e-12}
    s.preflight('extract',[request])
    with pytest.raises(ExpectedServiceError) as exc:s.call('extract',*request)
    assert exc.value.code=='llm_stage_cost_budget'
    assert len(seen)==s.calls==len(s.attempt_records)==2
    assert [record['http_status'] for record in s.attempt_records]==[400,429]
    assert all(record['usage_known'] is False for record in s.attempt_records)
    assert s.reserved_tokens==2*budget.total
    quota=s.quota_ledger.snapshot()
    assert quota['attempts_24h']==2 and quota['unknown_reserved_tokens']==2*budget.total
    assert quota['reported_tokens']==0


def test_known_usage_releases_stage_cost_for_retries_on_later_request(tmp_path):
    seen=[]
    def transport(payload,*args):
        seen.append(payload)
        if len(seen) in (1,3,4):raise failure(400,'json_validate_failed')
        if len(seen)==2:return response()
        pytest.fail('Another request cannot receive its own additional stage retry allowance')
    s=session(tmp_path,transport)
    requests=[('system',{'batch':number},EXTRACT_SCHEMA,100) for number in (1,2)]
    budgets=[s.budget('extract',*request) for request in requests]
    # Preflight reserves three full requests. A known 15-token success then
    # frees enough of that fixed ceiling for the second request's schema retry.
    reserved=2*budgets[0].total+budgets[1].total+15
    s.stage_cost_limits={'extract':reserved*max(PRICES[s.model])/1e6+1e-12}
    s.preflight('extract',requests)
    assert s.call('extract',*requests[0])=={}
    with pytest.raises(ExpectedServiceError) as exc:s.call('extract',*requests[1])
    assert exc.value.code=='invalid_llm_response' and s.calls==len(seen)==4
    assert [attempt['status'] for attempt in s.attempt_records]==[
        'http_error','complete','http_error','http_error']
    quota=s.quota_ledger.snapshot()
    assert quota['reported_tokens']==15
    assert quota['unknown_reserved_tokens']==budgets[0].total+2*budgets[1].total


def test_known_usage_settles_session_rate_gate_and_ledger_but_keeps_estimate_audit(tmp_path):
    gate = RateGate(enforce_window=True)
    s = GroqSession('synthetic-key', transport=lambda *_: response(), free_only=True,
        quota_evidence={'synthetic_fixture':True}, quota_job_id='known-settlement',
        quota_ledger=FreeQuotaLedger(tmp_path/'settled-quota.sqlite3'), rate_gate=gate)
    request=('system', {'synthetic':'source'}, EXTRACT_SCHEMA, 100)
    budget=s.budget('extract', *request)
    assert s.manifest()['token_accounting']=='reported_usage_plus_unknown_reservations'
    assert s.call('extract', *request)=={}
    attempt=s.attempt_records[0]
    assert attempt['total_reserved']==budget.total
    assert attempt['input_estimate']==budget.input_tokens
    assert attempt['output_reserved']==budget.output_tokens
    assert attempt['settled_tokens']==15 and attempt['released_tokens']==budget.total-15
    assert attempt['usage_known'] is True and 'usage_invalid' not in attempt
    assert s.reserved_tokens==15 and s.calls==1
    assert gate.reservations==[(gate.reservations[0][0],15)]
    assert gate.daily_reservations==[(gate.daily_reservations[0][0],15)]
    quota=s.quota_ledger.snapshot()
    assert quota['attempts_24h']==1 and quota['reported_tokens']==15
    assert quota['unknown_reserved_tokens']==0


def test_known_low_usage_allows_next_request_under_fixed_max_tokens(tmp_path):
    seen=[]
    s=session(tmp_path,lambda *args:(seen.append(args[0]) or response()))
    request=('system', {'synthetic':'source'}, EXTRACT_SCHEMA, 100)
    budget=s.budget('extract', *request)
    s.max_tokens=budget.total+200
    assert s.call('extract', *request)=={}
    assert s.reserved_tokens==15
    assert s.call('extract', *request)=={}
    assert len(seen)==s.calls==2 and s.reserved_tokens==30


def test_unknown_usage_keeps_full_tokens_and_blocks_next_request(tmp_path):
    seen=[]
    unknown={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
    s=session(tmp_path,lambda *args:(seen.append(args[0]) or unknown))
    request=('system', {'synthetic':'source'}, EXTRACT_SCHEMA, 100)
    budget=s.budget('extract', *request)
    s.max_tokens=budget.total+200
    assert s.call('extract', *request)=={}
    assert s.reserved_tokens==budget.total
    with pytest.raises(ExpectedServiceError) as exc:
        s.call('extract', *request)
    assert exc.value.code=='llm_budget_exceeded'
    assert len(seen)==s.calls==1


def test_known_zero_usage_settles_tokens_but_preserves_attempt_count(tmp_path):
    zero={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],
        'usage':{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}}
    s=session(tmp_path,lambda *_:zero)
    assert s.call('extract','system',{},EXTRACT_SCHEMA,100)=={}
    assert s.reserved_tokens==0 and s.calls==1
    assert s.attempt_records[0]['usage_known'] is True
    assert s.attempt_records[0]['settled_tokens']==0
    quota=s.quota_ledger.snapshot()
    assert quota['attempts_24h']==1 and quota['reported_tokens']==0
    assert quota['unknown_reserved_tokens']==0


@pytest.mark.parametrize('usage', [
    None,
    {},
    {'prompt_tokens':10},
    {'prompt_tokens':None,'completion_tokens':5},
    {'prompt_tokens':True,'completion_tokens':5},
    {'prompt_tokens':-1,'completion_tokens':5},
    {'prompt_tokens':10,'completion_tokens':5,'total_tokens':None},
    {'prompt_tokens':10,'completion_tokens':5,'total_tokens':14},
    {'prompt_tokens':10,'completion_tokens':5,'total_tokens':True},
])
def test_invalid_or_unknown_usage_retains_full_reservation(tmp_path, usage):
    result={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
    if usage is not None:
        result['usage']=usage
    s=session(tmp_path,lambda *_:result)
    budget=s.budget('extract','system',{},EXTRACT_SCHEMA,100)
    assert s.call('extract','system',{},EXTRACT_SCHEMA,100)=={}
    assert s.reserved_tokens==budget.total
    assert s.calls==1
    attempt=s.attempt_records[0]
    assert attempt['usage_known'] is False
    assert attempt['prompt_tokens']==attempt['completion_tokens']==attempt['total_tokens']==0
    assert attempt['settled_tokens'] is None
    if usage is not None:
        assert attempt['usage_invalid'] is True
    quota=s.quota_ledger.snapshot()
    assert quota['attempts_24h']==1 and quota['unknown_reserved_tokens']==budget.total
    assert quota['reported_tokens']==0


def test_cap_blocks_first_attempt_without_preflight_or_creating_quota_usage(tmp_path):
    s=session(tmp_path,lambda *_:pytest.fail('Over-cap attempt must not call transport'))
    request=('system',{},EXTRACT_SCHEMA,100)
    s.stage_cost_limits={'extract':s.budget('extract',*request).total*max(PRICES[s.model])/2e6}
    with pytest.raises(ExpectedServiceError) as exc:s.call('extract',*request)
    assert exc.value.code=='llm_stage_cost_budget'
    assert s.calls==0 and s.attempt_records==[] and s.reserved_tokens==0
    assert s.quota_ledger.snapshot()['attempts_24h']==0
