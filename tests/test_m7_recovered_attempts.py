"""A recovered provider error preserves consumption and audited control gates."""
from copy import deepcopy
import json

import pytest

from suno_asset import m7_acceptance as acceptance


@pytest.mark.parametrize('summary,expected',[
    ({'paid':True},True),
    ({'paid':False},False),
    ({'paid':False,'provider_calls_real':True,'quota_mode':'free_only','authorized_billing_usd':0},True),
    ({'paid':False,'provider_calls_real':False,'quota_mode':'simulated','authorized_billing_usd':0},False),
    ({'paid':True,'provider_calls_real':False},False),
    ({'paid':True,'provider_calls_real':True,'quota_mode':'free_only','authorized_billing_usd':0},False),
    ({'paid':False,'provider_calls_real':True,'quota_mode':'free_only','authorized_billing_usd':False},False),
    ({'paid':False,'provider_calls_real':True,'quota_mode':'free_only','authorized_billing_usd':1},False),
    ({'paid':False,'provider_calls_real':True},False),
])
def test_real_free_inference_is_distinct_from_billing_and_simulation(summary,expected):
    assert acceptance.provider_calls_are_real(summary) is expected


def trace(tmp_path, statuses):
    model = 'openai/gpt-oss-120b'
    payload = {'model':model,'messages':[]}
    payload_hash = __import__('hashlib').sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    attempts=[]
    for index,status in enumerate(statuses,1):
        attempt={'attempt':index,'stage':'evaluate','model':model,'payload_sha256':payload_hash,
            'status':status[0],'usage_known':status[0]=='complete','input_estimate':123,'output_reserved':456}
        def save(kind,value):
            (tmp_path/f'attempt-{index:03}-{kind}.json').write_text(json.dumps(value))
        save('started',{'manifest':{'models':{'evaluate':model}},'attempt':deepcopy(attempt),'request':payload})
        if status[0]=='complete':
            save('received',{'response':{'usage':{'prompt_tokens':123,'completion_tokens':50}}})
            attempt.update(prompt_tokens=123,completion_tokens=50)
        else:
            attempt.update(http_status=status[1],provider_code=status[2])
            save('interrupted',{'http_status':status[1],'usage_known':False,
                'provider_response':json.dumps({'error':{'code':status[2]}})})
        attempts.append(attempt)
    return {'configuration_id':'literal-quality','audit_dir':str(tmp_path),'audit_hashes':{
        str(path.resolve()):acceptance.digest(path) for path in tmp_path.glob('*.json')},
        'attempts':attempts,'usage_complete':all(a['usage_known'] for a in attempts)}


@pytest.mark.parametrize('errors',[
    [('http_error',400,'json_validate_failed')],
    [('http_error',429,'rate_limit_exceeded')],
    [('http_error',400,'json_validate_failed'),('http_error',429,'rate_limit_exceeded')],
])
def test_recovered_retry_completes_control_without_erasing_unknown_reserve(tmp_path,errors):
    run=trace(tmp_path,[*errors,('complete',)])
    before=deepcopy(run)
    completed=acceptance.validate_completed_attempts(run)
    assert completed==[run['attempts'][-1]]
    assert acceptance.technical_attempts_complete(run)
    assert run==before and not run['usage_complete']
    assert sum(a['input_estimate']+a['output_reserved'] for a in run['attempts'])==579*len(run['attempts'])


@pytest.mark.parametrize('errors',[
    [('http_error',503,'unclassified')],
    [('unavailable',None,'unclassified')],
    [('http_error',400,'invalid_request_error')],
    [('http_error',429,'unclassified')],
    [('http_error',400,'json_validate_failed'),('http_error',400,'json_validate_failed')],
    [('http_error',429,'rate_limit_exceeded'),('http_error',429,'rate_limit_exceeded')],
])
def test_unrecoverable_error_or_retry_limit_blocks_even_after_success(tmp_path,errors):
    run=trace(tmp_path,[*errors,('complete',)])
    assert not acceptance.technical_attempts_complete(run)
    with pytest.raises(ValueError):acceptance.validate_completed_attempts(run)


def test_retry_requires_complete_control_and_identical_request(tmp_path):
    run=trace(tmp_path,[('http_error',400,'json_validate_failed')])
    with pytest.raises(ValueError,match='posterior'):acceptance.validate_completed_attempts(run)


def test_retry_cannot_recover_a_different_payload(tmp_path):
    run=trace(tmp_path,[('http_error',400,'json_validate_failed'),('complete',)])
    path=tmp_path/'attempt-002-started.json'
    checkpoint=json.loads(path.read_text())
    checkpoint['request']['messages']=[{'role':'user','content':'different'}]
    changed_hash=__import__('hashlib').sha256(json.dumps(checkpoint['request'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    checkpoint['attempt']['payload_sha256']=changed_hash
    run['attempts'][1]['payload_sha256']=changed_hash
    path.write_text(json.dumps(checkpoint))
    run['audit_hashes'][str(path.resolve())]=acceptance.digest(path)
    with pytest.raises(ValueError,match='diverge do pedido'):acceptance.validate_completed_attempts(run)


def test_recovered_error_cannot_claim_complete_usage(tmp_path):
    run=trace(tmp_path,[('http_error',429,'rate_limit_exceeded'),('complete',)])
    run['usage_complete']=True
    with pytest.raises(ValueError,match='Uso desconhecido'):acceptance.validate_completed_attempts(run)


def test_unknown_consumption_must_keep_entire_original_reservation(tmp_path):
    run=trace(tmp_path,[('http_error',400,'json_validate_failed'),('complete',)])
    run['attempts'][0]['input_estimate']-=1
    with pytest.raises(ValueError,match='reserva integral'):acceptance.validate_completed_attempts(run)


def test_retry_requires_untampered_provider_error_checkpoint(tmp_path):
    run=trace(tmp_path,[('http_error',400,'json_validate_failed'),('complete',)])
    interrupted=tmp_path/'attempt-001-interrupted.json'
    interrupted.write_text(json.dumps({'http_status':400,'usage_known':False,
        'provider_response':json.dumps({'error':{'code':'invalid_request_error'}})}))
    with pytest.raises(ValueError,match='Auditoria'):acceptance.validate_completed_attempts(run)
    run['audit_hashes'][str(interrupted.resolve())]=acceptance.digest(interrupted)
    with pytest.raises(ValueError,match='Retry não permitido'):acceptance.validate_completed_attempts(run)
