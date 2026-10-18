"""Offline source-first acceptance proofs, with synthetic source and audit."""
import json
from hashlib import sha256
from pathlib import Path

import pytest

from suno_asset import m7_acceptance as acceptance
from suno_asset.domain.models import ExecutionResult
from suno_asset.services.literal import block_payload
from test_m7_automated_acceptance import paid_result_artifact, content_for, save


@pytest.fixture
def source_run(tmp_path):
    run = paid_result_artifact(tmp_path, config_name='literal-quality', source_id='synthetic',
        source_sha='a'*64, content_value=content_for('beginner','analytical_text'),
        audience='beginner', output_format='analytical_text',
        stages=('extract','verify','verify','generate','evaluate'))
    result = json.loads(Path(run['result_file']).read_text())
    result['manifest']['prompt_version'] = 'literal-v1.10'
    source_check = {'block_id':'B1','classification':'material',
        'material_excerpt_ids':['E1'],'reason':'A fonte contém fato material.'}
    result['fact_verification'].update(source_materiality_complete=True, source_materiality_checks=[source_check])
    typed = ExecutionResult.model_validate(result)
    manifest = result['manifest']
    run['manifest'] = manifest
    budget = json.loads(Path(run['budget_file']).read_text())
    budget['manifest'] = manifest
    audit_dir = Path(run['audit_dir'])
    source_payload = block_payload(typed.prepared_source, typed.prepared_source.blocks)
    anchor = typed.extraction.anchors[0]
    comparison = {**source_payload,
        'processing':[p.model_dump(mode='json') for p in typed.extraction.processing],
        'anchors':[{'id':anchor.anchor_id,'kind':anchor.kind.value,'importance':anchor.importance.value,
            'owner_block_id':anchor.owner_block_id,'excerpt_ids':['E1'],'numeric_facts':[]}]}
    source_output = {'blocks':[source_check]}
    comparison_output = {'blocks':[b.model_dump(mode='json') for b in typed.fact_verification.block_verifications]}
    for index, attempt in enumerate(budget['attempts'], 1):
        path = audit_dir/f'attempt-{index:03}-started.json'
        started = json.loads(path.read_text())
        started['manifest'] = manifest
        if index in (2,3):
            payload = {**source_payload,'control':'source_materiality'} if index == 2 else comparison
            started['request']['messages'] = [{'role':'user','content':json.dumps(payload)}]
            started['request']['response_format'] = {'json_schema':{'name':'suno_verify_literal-v1_10'}}
            attempt['payload_sha256'] = sha256(json.dumps(started['request'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            output = source_output if index == 2 else comparison_output
            save(audit_dir/f'attempt-{index:03}-received.json',{'response':{'usage':{'prompt_tokens':1,'completion_tokens':1},
                'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]}})
        started['attempt'] = attempt
        save(path, started)
    budget['events'] = [{'stage':'source_materiality','block_ids':['B1'],'output':source_output},
        {'stage':'block_verification','output':comparison_output}]
    save(Path(run['result_file']),result)
    save(Path(run['budget_file']),budget)
    reconcile(run)
    return run


def reconcile(run):
    run['result_sha256'] = acceptance.digest(run['result_file'])
    run['budget_sha256'] = acceptance.digest(run['budget_file'])
    run['audit_hashes'] = {str(p.resolve()):acceptance.digest(p) for p in Path(run['audit_dir']).glob('*.json')}


def test_source_first_complete_audit_passes(source_run):
    assert acceptance.validate_paid_execution(source_run).status.value == 'approved'


@pytest.mark.parametrize('broken', ['missing_flag','missing_rows','legacy_defaults','changed_reason','missing_event',
    'event_after_comparison','response_tamper','candidate_in_source','source_text_tamper','comparison_tamper'])
def test_source_first_missing_or_tampered_evidence_blocks(source_run, broken):
    result_path = Path(source_run['result_file']); budget_path = Path(source_run['budget_file'])
    result = json.loads(result_path.read_text()); budget = json.loads(budget_path.read_text())
    if broken == 'missing_flag': result['fact_verification']['source_materiality_complete'] = None
    elif broken == 'missing_rows': result['fact_verification']['source_materiality_checks'] = []
    elif broken == 'legacy_defaults':
        result['fact_verification'].pop('source_materiality_complete')
        result['fact_verification'].pop('source_materiality_checks')
    elif broken == 'changed_reason': result['fact_verification']['source_materiality_checks'][0]['reason'] = 'fabricated'
    elif broken == 'missing_event': budget['events'] = budget['events'][1:]
    elif broken == 'event_after_comparison': budget['events'].reverse()
    elif broken == 'response_tamper':
        path = Path(source_run['audit_dir'])/'attempt-002-received.json'
        response = json.loads(path.read_text()); response['response']['choices'][0]['message']['content'] = '{"blocks":[]}'
        save(path,response)
    else:
        index = 3 if broken == 'comparison_tamper' else 2
        path = Path(source_run['audit_dir'])/f'attempt-{index:03}-started.json'
        started = json.loads(path.read_text()); payload = json.loads(started['request']['messages'][0]['content'])
        if broken == 'candidate_in_source': payload['processing'] = []
        elif broken == 'source_text_tamper': payload['excerpts'][0]['text'] = 'changed'
        else: payload['anchors'][0]['excerpt_ids'] = []
        started['request']['messages'][0]['content'] = json.dumps(payload)
        hashed = sha256(json.dumps(started['request'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        started['attempt']['payload_sha256'] = hashed; budget['attempts'][index-1]['payload_sha256'] = hashed
        save(path, started)
    save(result_path,result); save(budget_path,budget); reconcile(source_run)
    with pytest.raises(ValueError,match='source-first|source|Comparação|source_materiality|materialidade da fonte'):
        acceptance.validate_paid_execution(source_run)


def test_legacy_literal_artifact_remains_readable(tmp_path):
    run = paid_result_artifact(tmp_path, config_name='literal-quality', source_id='synthetic',source_sha='b'*64,
        content_value=content_for('beginner','analytical_text'),audience='beginner',output_format='analytical_text')
    assert acceptance.validate_paid_execution(run).status.value == 'approved'


@pytest.mark.parametrize('broken',['not_verified','foreign_excerpt','duplicate_block','missing_comparison'])
def test_source_first_unverified_or_incomplete_blocks(source_run,broken):
    path = Path(source_run['result_file']); result = json.loads(path.read_text())
    checks = result['fact_verification']['source_materiality_checks']
    if broken == 'not_verified':
        checks[0].update(classification='not_verified',material_excerpt_ids=[])
        result['fact_verification']['approved'] = False
        result['status'] = source_run['status'] = 'needs_review'
    elif broken == 'foreign_excerpt': checks[0]['material_excerpt_ids'] = ['E999']
    elif broken == 'duplicate_block': checks.append(dict(checks[0]))
    else:
        budget_path = Path(source_run['budget_file']); budget = json.loads(budget_path.read_text())
        budget['events'] = budget['events'][:1]
        save(budget_path,budget)
    save(path,result); reconcile(source_run)
    with pytest.raises(ValueError): acceptance.validate_paid_execution(source_run)


@pytest.mark.parametrize('candidate',['administrative','no_relevant_facts'])
def test_source_material_cannot_be_hidden_by_candidate_classification(source_run,candidate):
    result_path = Path(source_run['result_file']); result = json.loads(result_path.read_text())
    result['extraction']['processing'][0].update(classification=candidate,anchor_ids=[])
    result['fact_verification']['block_verifications'][0]['anchors'] = []
    result['fact_verification']['approved'] = False
    result['status'] = source_run['status'] = 'needs_review'
    # Reconcile the exact candidate comparison, preserving the separate source
    # verdict. A fabricated passing report must still fail the gate.
    path = Path(source_run['audit_dir'])/'attempt-003-started.json'; started = json.loads(path.read_text())
    payload = json.loads(started['request']['messages'][0]['content'])
    payload['processing'] = result['extraction']['processing']; payload['anchors'] = []
    started['request']['messages'][0]['content'] = json.dumps(payload)
    hashed = sha256(json.dumps(started['request'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    started['attempt']['payload_sha256'] = hashed; save(path,started)
    budget_path = Path(source_run['budget_file']); budget = json.loads(budget_path.read_text())
    budget['attempts'][2]['payload_sha256'] = hashed
    output = {'blocks':[result['fact_verification']['block_verifications'][0]]}
    save(Path(source_run['audit_dir'])/'attempt-003-received.json',{'response':{
        'usage':{'prompt_tokens':1,'completion_tokens':1},
        'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]}})
    budget['events'][1]['output'] = output
    save(budget_path,budget); save(result_path,result); reconcile(source_run)
    with pytest.raises(ValueError,match='Parecer de blocos source-first|Fonte material'):
        acceptance.validate_paid_execution(source_run)
    result['fact_verification']['block_verifications'][0].update(
        coverage='failed',relevance='failed',omission_excerpt_ids=['E1'])
    save(result_path,result); reconcile(source_run)
    assert acceptance.validate_paid_execution(source_run).status.value == 'needs_review'


@pytest.mark.parametrize('broken',['audit_order','schema_version','event_batch_ids'])
def test_source_first_requires_audited_phase_order_and_version(source_run,broken):
    budget_path = Path(source_run['budget_file']); budget = json.loads(budget_path.read_text())
    audit = Path(source_run['audit_dir'])
    starts = [json.loads((audit/f'attempt-{index:03}-started.json').read_text()) for index in (2,3)]
    if broken == 'event_batch_ids': budget['events'][0]['block_ids'] = ['B999']
    elif broken == 'schema_version':
        starts[0]['request']['response_format']['json_schema']['name'] = 'suno_verify_literal-v1_9'
    else:
        starts[0]['request'],starts[1]['request'] = starts[1]['request'],starts[0]['request']
        responses = [(audit/f'attempt-{index:03}-received.json').read_text() for index in (2,3)]
        (audit/'attempt-002-received.json').write_text(responses[1])
        (audit/'attempt-003-received.json').write_text(responses[0])
    for index,started in zip((2,3),starts):
        hashed = sha256(json.dumps(started['request'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        started['attempt']['payload_sha256'] = hashed
        budget['attempts'][index-1]['payload_sha256'] = hashed
        save(audit/f'attempt-{index:03}-started.json',started)
    save(budget_path,budget); reconcile(source_run)
    with pytest.raises(ValueError,match='source-first|Comparação'):
        acceptance.validate_paid_execution(source_run)
