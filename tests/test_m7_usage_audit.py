"""Provider usage must support the counters and settlements used by M7 gates."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from suno_asset import m7_acceptance as acceptance


def trace(tmp_path, usage, *, known=True):
    model = 'openai/gpt-oss-120b'
    payload = {'model':model,'messages':[],'max_completion_tokens':100}
    attempt = {'attempt':1,'stage':'evaluate','model':model,'status':'complete',
        'input_estimate':200,'output_reserved':100,'total_reserved':300,
        'payload_sha256':sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        'usage_known':known,'prompt_tokens':120 if known else 0,
        'completion_tokens':20 if known else 0,'settled_tokens':140 if known else None,
        'released_tokens':160 if known else 0}
    if not known and isinstance(usage,dict):
        attempt['usage_invalid'] = True
        if 'total_tokens' in usage:
            attempt['total_tokens'] = usage['total_tokens']
    (tmp_path/'attempt-001-started.json').write_text(json.dumps({
        'manifest':{'models':{'evaluate':model}},'attempt':deepcopy(attempt),'request':payload}))
    response = {'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],'usage':usage}
    (tmp_path/'attempt-001-received.json').write_text(json.dumps({'response':response}))
    run = {'configuration_id':'literal-quality','audit_dir':str(tmp_path),
        'attempts':[attempt],'usage_complete':known}
    rehash(run)
    return run


def rehash(run):
    from pathlib import Path
    run['audit_hashes'] = {str(path.resolve()):acceptance.digest(path)
        for path in Path(run['audit_dir']).glob('*.json')}


def test_known_counters_and_settlement_match_received_usage(tmp_path):
    run = trace(tmp_path, {'prompt_tokens':120,'completion_tokens':20,'total_tokens':140})
    before = deepcopy(run)
    assert acceptance.validate_completed_attempts(run) == run['attempts']
    assert run == before


@pytest.mark.parametrize('field,value', [
    ('prompt_tokens',None),('prompt_tokens',True),('prompt_tokens',-1),
    ('prompt_tokens','120'),('prompt_tokens',121),('completion_tokens',False),
    ('completion_tokens',21),('total_tokens',None),('total_tokens',False),
    ('total_tokens',-1),('total_tokens','140'),('total_tokens',141),
    ('usage_known',False),('usage_known',1),('settled_tokens',True),
    ('settled_tokens',139),('released_tokens',161),('released_tokens',False),
    ('usage_invalid',True),('usage_invalid',0),('total_reserved',299),
])
def test_summary_cannot_forge_known_usage_or_release(tmp_path, field, value):
    run = trace(tmp_path, {'prompt_tokens':120,'completion_tokens':20,'total_tokens':140})
    run['attempts'][0][field] = value
    with pytest.raises(ValueError):
        acceptance.validate_completed_attempts(run)


@pytest.mark.parametrize('usage', [
    None,{}, {'prompt_tokens':120},
    {'prompt_tokens':None,'completion_tokens':20},
    {'prompt_tokens':True,'completion_tokens':20},
    {'prompt_tokens':120,'completion_tokens':False},
    {'prompt_tokens':-1,'completion_tokens':20},
    {'prompt_tokens':'120','completion_tokens':20},
    {'prompt_tokens':120,'completion_tokens':20,'total_tokens':None},
    {'prompt_tokens':120,'completion_tokens':20,'total_tokens':True},
    {'prompt_tokens':120,'completion_tokens':20,'total_tokens':139},
])
def test_hash_bound_invalid_provider_usage_cannot_be_claimed_known(tmp_path, usage):
    # Even a correctly bound response hash cannot make invalid provider
    # counters into known consumption or justify releasing a reservation.
    run = trace(tmp_path, usage)
    with pytest.raises(ValueError,match='Consumo conhecido'):
        acceptance.validate_completed_attempts(run)


@pytest.mark.parametrize('usage', [None,{}, {'prompt_tokens':None,'completion_tokens':20},
    {'prompt_tokens':True,'completion_tokens':20},
    {'prompt_tokens':120,'completion_tokens':20,'total_tokens':139}])
def test_unknown_provider_usage_retains_full_reserve_and_incomplete_usage(tmp_path, usage):
    run = trace(tmp_path, usage, known=False)
    assert acceptance.validate_completed_attempts(run) == run['attempts']
    assert run['usage_complete'] is False
    assert run['attempts'][0]['settled_tokens'] is None
    assert run['attempts'][0]['released_tokens'] == 0
    run['usage_complete'] = True
    with pytest.raises(ValueError,match='Uso desconhecido'):
        acceptance.validate_completed_attempts(run)


@pytest.mark.parametrize('field,value', [('prompt_tokens',True),('prompt_tokens',-1),
    ('completion_tokens',20),('settled_tokens',0),('released_tokens',1),
    ('released_tokens',False),('total_tokens',1),('usage_invalid',False)])
def test_unknown_usage_cannot_fabricate_metrics_or_settlement(tmp_path, field, value):
    run = trace(tmp_path, {}, known=False)
    run['attempts'][0][field] = value
    with pytest.raises(ValueError):
        acceptance.validate_completed_attempts(run)


def test_received_usage_change_requires_hash_and_counter_reconciliation(tmp_path):
    run = trace(tmp_path, {'prompt_tokens':120,'completion_tokens':20})
    path = tmp_path/'attempt-001-received.json'
    response = json.loads(path.read_text())
    response['response']['usage']['prompt_tokens'] = 121
    path.write_text(json.dumps(response))
    with pytest.raises(ValueError,match='Auditoria'):
        acceptance.validate_completed_attempts(run)
    rehash(run)
    with pytest.raises(ValueError,match='Contadores conhecidos'):
        acceptance.validate_completed_attempts(run)


def test_reported_zero_is_known_and_preserves_attempt_count(tmp_path):
    run = trace(tmp_path, {'prompt_tokens':0,'completion_tokens':0,'total_tokens':0})
    run['attempts'][0].update(prompt_tokens=0,completion_tokens=0,settled_tokens=0,released_tokens=300)
    assert len(acceptance.validate_completed_attempts(run)) == 1
    assert run['usage_complete'] is True


def test_legacy_known_record_without_optional_settlement_still_needs_real_counters(tmp_path):
    run = trace(tmp_path, {'prompt_tokens':120,'completion_tokens':20})
    row = run['attempts'][0]
    row.pop('settled_tokens'); row.pop('released_tokens'); row.pop('total_reserved')
    assert acceptance.validate_completed_attempts(run) == run['attempts']
    row.pop('prompt_tokens')
    with pytest.raises(ValueError,match='Contadores conhecidos'):
        acceptance.validate_completed_attempts(run)


def test_underflow_cannot_be_forged_as_completed_control(tmp_path):
    run = trace(tmp_path, {'prompt_tokens':201,'completion_tokens':20})
    run['attempts'][0].update(prompt_tokens=201,settled_tokens=221,released_tokens=79)
    with pytest.raises(ValueError,match='acima da estimativa'):
        acceptance.validate_completed_attempts(run)


@pytest.mark.parametrize('usage',[None,{}, {'prompt_tokens':True,'completion_tokens':5},
    {'prompt_tokens':10,'completion_tokens':5,'total_tokens':None},
    {'prompt_tokens':10,'completion_tokens':5,'total_tokens':True},
    {'prompt_tokens':10,'completion_tokens':5,'total_tokens':139}])
def test_real_session_invalid_usage_keeps_unknown_reserve_and_audited_control(tmp_path, usage):
    from suno_asset.m7_literal_eval import audited_transport
    from suno_asset.services.groq import GroqSession
    from suno_asset.services.llm_budget import RateGate
    response = {'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],'usage':usage}
    session = GroqSession('synthetic-key',transport=lambda *_:response,
        rate_gate=RateGate(enforce_window=False))
    session.transport = audited_transport(session,tmp_path)
    request = ('system',{}, {'type':'object'},100)
    budget = session.budget('evaluate',*request)
    assert session.call('evaluate',*request) == {}
    run = {'configuration_id':'current-quality','audit_dir':str(tmp_path/'call-audit'),
        'attempts':session.attempt_records,'usage_complete':False}
    rehash(run)
    assert acceptance.technical_attempts_complete(run)
    assert session.attempt_records[0]['usage_known'] is False
    assert session.attempt_records[0]['settled_tokens'] is None
    assert session.attempt_records[0]['released_tokens'] == 0
    assert session.reserved_tokens == budget.total
    assert session.rate_gate.reservations[0][1] == budget.total


def paid_run(tmp_path, configuration='literal-quality'):
    from test_m7_automated_acceptance import paid_result_artifact, content_for
    return paid_result_artifact(tmp_path,config_name=configuration,source_id='synthetic',source_sha='a'*64,
        content_value=content_for('beginner','analytical_text'),audience='beginner',output_format='analytical_text')


@pytest.mark.parametrize('configuration',['current-quality','literal-quality','literal-hybrid'])
def test_paid_execution_recomputes_weighted_usage_cost_for_every_profile(tmp_path, configuration):
    from pathlib import Path
    from suno_asset.m7_eval import PRICES
    run = paid_run(tmp_path,configuration)
    budget = json.loads(Path(run['budget_file']).read_text())
    assert acceptance.validate_paid_execution(run).status.value == 'approved'
    expected = sum((PRICES[a['model']][0] + PRICES[a['model']][1]) / 1e6
        for a in budget['attempts'])
    assert run['estimated_usd'] == round(expected,6)
    assert run['prompt_tokens'] == run['completion_tokens'] == 4
    assert run['per_stage']['extract']['estimated_usd'] == sum(PRICES[run['configuration']['models']['extract']]) / 1e6


@pytest.mark.parametrize('fraud',[
    'prompt_total','completion_bool','attempt_count_bool','usage_flag','known_cost_zero',
    'nonfinite_cost','stage_call_count','stage_counter','stage_model','stage_price',
    'budget_and_run_stage_price','budget_and_run_missing_stage',
])
def test_rehashed_summaries_cannot_forge_aggregates_used_for_selection(tmp_path, fraud):
    from pathlib import Path
    run = paid_run(tmp_path)
    path = Path(run['budget_file'])
    budget = json.loads(path.read_text())
    if fraud == 'prompt_total':run['prompt_tokens'] = 0
    elif fraud == 'completion_bool':run['completion_tokens'] = True
    elif fraud == 'attempt_count_bool':run['attempts'] = True
    elif fraud == 'usage_flag':run['usage_complete'] = False
    elif fraud == 'known_cost_zero':run['estimated_usd'] = 0
    elif fraud == 'nonfinite_cost':run['estimated_usd'] = float('nan')
    elif fraud == 'stage_call_count':run['per_stage']['extract']['calls'] = 2
    elif fraud == 'stage_counter':run['per_stage']['extract']['prompt_tokens'] = 0
    elif fraud == 'stage_model':run['per_stage']['extract']['models'] = ['openai/gpt-oss-20b']
    elif fraud == 'stage_price':run['per_stage']['extract']['estimated_usd'] = 0
    elif fraud == 'budget_and_run_stage_price':
        budget['per_stage']['extract']['estimated_usd'] = run['per_stage']['extract']['estimated_usd'] = 0
        run['estimated_usd'] = round(sum(r['estimated_usd'] for r in run['per_stage'].values()),6)
    elif fraud == 'budget_and_run_missing_stage':
        budget['per_stage'].pop('extract');run['per_stage'].pop('extract')
    path.write_text(json.dumps(budget));run['budget_sha256'] = acceptance.digest(path)
    with pytest.raises(ValueError):
        acceptance.validate_paid_execution(run)


def test_completed_unknown_usage_keeps_conservative_selection_flag_and_reported_cost_only(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace
    from suno_asset.m7_literal_eval import cost_records
    run = paid_run(tmp_path)
    path = Path(run['budget_file']);budget = json.loads(path.read_text())
    row = budget['attempts'][-1]
    row.update(usage_known=False,prompt_tokens=0,completion_tokens=0,total_tokens=0,
        settled_tokens=None,released_tokens=0,usage_invalid=True)
    received = Path(run['audit_dir'])/'attempt-004-received.json'
    raw = json.loads(received.read_text());raw['response']['usage'] = {}
    received.write_text(json.dumps(raw));rehash(run)
    reported = [attempt for attempt in budget['attempts'] if attempt['usage_known']]
    budget['per_stage'] = cost_records(SimpleNamespace(records=reported))
    path.write_text(json.dumps(budget));run['budget_sha256'] = acceptance.digest(path)
    run.update(prompt_tokens=3,completion_tokens=3,usage_complete=False,per_stage=budget['per_stage'],
        estimated_usd=round(sum(r['estimated_usd'] for r in budget['per_stage'].values()),6))
    assert acceptance.validate_paid_execution(run).status.value == 'approved'
    assert run['usage_complete'] is False
    assert row['settled_tokens'] is None and row['released_tokens'] == 0
    assert row['input_estimate'] + row['output_reserved'] == 2
