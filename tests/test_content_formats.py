"""Audience/format matrix, strict text provenance and real-schema offline contracts."""
from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from suno_asset.api.app import create_app
from suno_asset.domain.models import (
    Audience, CarouselContent, ContentBlock, Claim, CarouselCard, ExecutionResult, GeneratedContent, OutputFormat,
)
from suno_asset.services.content_formats import format_errors, validate_content
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.literal import LiteralGenerator, SimulatedLiteralGenerator
from suno_asset.services.groq import GroqRefiner
from test_literal_pipeline import prepared, session
from suno_asset.services.literal import SimulatedLiteralExtractor
from suno_asset.services.source_preparation import source_artifacts
from suno_asset.domain.models import GenerationRequest

SOURCE = ('1. A atividade desacelerou.\n2. A inflação permaneceu elevada.\n'
          '3. O consumo diminuiu.\n4. Há riscos externos.\n5. Não há antecipação de cortes.')


def test_economy_limits_analytical_text_to_500_words_and_carousel_to_six_total_screens():
    claim = Claim(claim_id='c1', text='fato')
    long = ' '.join(['palavra'] * 501)
    analytical = GeneratedContent(content_id=uuid4(), revision=0, audience='beginner',
        title='Análise da ata do Copom', synthesis=long, development='desenvolvimento',
        implications='implicações', claims=(claim,))
    carousel = CarouselContent(content_id=uuid4(), revision=0, audience='intermediate',
        title='Carrossel', claims=(claim,), hook=ContentBlock(text='fato', claim_ids=('c1',)),
        cards=tuple(CarouselCard(order=i, text='fato', claim_ids=('c1',)) for i in range(1, 6)),
        conclusion=ContentBlock(text='fato', claim_ids=('c1',)))

    assert format_errors(analytical, economy=True)[0].code == 'format_noncompliant'
    assert not format_errors(analytical)
    assert format_errors(carousel, economy=True)[0].code == 'format_noncompliant'
    assert not format_errors(carousel)


@pytest.mark.parametrize('audience', list(Audience))
@pytest.mark.parametrize('output_format', list(OutputFormat))
def test_nine_literal_combinations_are_persisted_and_only_requested_format(tmp_path, audience, output_format):
    storage = LocalStorage(tmp_path)
    client = TestClient(create_app(storage=storage))
    source = client.post('/documents', json={'text':SOURCE}).json()
    result = client.post('/generations', json={'source_id':source['source_id'], 'architecture':'literal-v1',
        'audience':audience.value, 'output_format':output_format.value}).json()
    assert result['status'] == 'approved'
    content = result['content']
    assert content['audience'] == audience.value and content['output_format'] == output_format.value
    assert ('synthesis' in content) == (output_format == OutputFormat.ANALYTICAL_TEXT)
    assert ('cards' in content) == (output_format == OutputFormat.INFORMATIONAL_CAROUSEL)
    assert ('spoken_blocks' in content) == (output_format == OutputFormat.SHORT_VIDEO_SCRIPT)
    parsed = ExecutionResult.model_validate_json(ExecutionResult.model_validate(result).model_dump_json())
    assert storage.load_execution_result(parsed.request_id) == parsed
    restarted = TestClient(create_app(storage=storage))
    assert restarted.get(f'/generations/{parsed.request_id}').json() == result
    assert restarted.get(f'/generations/{parsed.request_id}/evidence/0').status_code == 200
    assert result['manifest']['resolved_configuration']['output_format'] == output_format.value


def test_capabilities_and_historical_architecture_limit(tmp_path):
    client = TestClient(create_app(storage=LocalStorage(tmp_path)))
    capabilities = client.get('/capabilities').json()
    assert len(capabilities['architectures']['literal-v1']['combinations']) == 9
    assert len(capabilities['architectures']['current']['combinations']) == 1
    assert capabilities['defaults']['audience'] == 'beginner'
    assert capabilities['defaults']['output_format'] == 'analytical_text'
    assert capabilities['promoted_configuration'] is None
    source = client.post('/documents', json={'text':SOURCE}).json()
    assert client.post('/generations', json={'source_id':source['source_id'], 'audience':'advanced'}).status_code == 422


def test_mvp_release_sets_economy_default_and_hash_checks_outputs(tmp_path,monkeypatch):
    monkeypatch.setenv('SUNO_ECONOMY_PROVIDER','groq')
    import json
    from hashlib import sha256
    from suno_asset.config import Settings
    outputs={}
    for fmt in ('analytical_text','informational_carousel','short_video_script'):
        path=tmp_path/f'{fmt}.json';path.write_text('{"reviewed":true}')
        outputs[fmt]={'path':path.name,'sha256':sha256(path.read_bytes()).hexdigest()}
    release=tmp_path/'mvp-release.json'
    release.write_text(json.dumps({'version':'m7-functional-mvp-release-v1','status':'accepted',
        'configuration':{'architecture':'literal-v1','model_profile':'economy'},'quality_review':'approved','outputs':outputs}))
    app=create_app(storage=LocalStorage(tmp_path/'store'),settings=Settings(mvp_release_path=release))
    caps=next(route.endpoint for route in app.routes if getattr(route,'path',None)=='/capabilities')()
    assert caps['defaults']['architecture']=='literal-v1' and caps['defaults']['model_profile']=='economy'
    outputs['analytical_text']['sha256']='0'*64
    release.write_text(json.dumps({'version':'m7-functional-mvp-release-v1','status':'accepted',
        'configuration':{'architecture':'literal-v1','model_profile':'economy'},'quality_review':'approved','outputs':outputs}))
    with pytest.raises(Exception):
        next(route.endpoint for route in app.routes if getattr(route,'path',None)=='/capabilities')()


def test_infeasible_card_count_and_duration_keep_inspectable_source_facts(tmp_path):
    client = TestClient(create_app(storage=LocalStorage(tmp_path)))
    for text, fmt in [('A atividade diminuiu.', 'informational_carousel'),
                      ('A atividade ' + 'desacelerou ' * 140 + '.', 'short_video_script')]:
        source = client.post('/documents', json={'text':text}).json()
        result = client.post('/generations', json={'source_id':source['source_id'], 'architecture':'literal-v1',
            'output_format':fmt}).json()
        assert result['status'] == 'needs_review' and result['content'] is not None
        assert any(issue['code'] == 'format_noncompliant' for issue in result['evaluation']['issues'])
        assert result['evaluation']['deferred_due_to_format'] is True
        assert not result['evaluation']['approved']
        assert text == result['content']['claims'][0]['text']


def test_strict_display_binding_and_duration(tmp_path):
    source = prepared(SOURCE)
    extraction = SimulatedLiteralExtractor().extract_prepared(source)
    _, evidence = source_artifacts(source)
    request = GenerationRequest(request_id=uuid4(), source=LocalStorage(tmp_path).put_text(SOURCE),
        architecture='literal-v1', audience='advanced', output_format='informational_carousel')
    content = SimulatedLiteralGenerator().generate(request, extraction.anchors, evidence)
    assert len(content.cards) == 5
    changed = content.model_dump(mode='json')
    changed['cards'][0]['text'] = 'Uma invenção factual.'
    with pytest.raises(ValidationError):
        validate_content(changed)
    changed = content.model_dump(mode='json')
    changed['cards'][0]['claim_ids'] = ['missing']
    with pytest.raises(ValidationError):
        validate_content(changed)
    video = SimulatedLiteralGenerator().generate(request.model_copy(update={'output_format':OutputFormat.SHORT_VIDEO_SCRIPT}), extraction.anchors, evidence)
    changed = video.model_dump(mode='json')
    changed['estimated_duration_seconds'] = 1
    with pytest.raises(ValidationError):
        validate_content(changed)
    assert video.estimated_duration_seconds == pytest.approx(video.computed_duration_seconds)


@pytest.mark.parametrize('fmt', ['informational_carousel','short_video_script'])
@pytest.mark.parametrize('audience', ['beginner','intermediate','advanced'])
def test_real_writer_uses_requested_schema_and_refines_same_format_offline(tmp_path, fmt, audience):
    source = prepared(SOURCE)
    extraction = SimulatedLiteralExtractor().extract_prepared(source)
    _, evidence = source_artifacts(source)
    seen = []
    def formatted(value, data):
        blocks = value['development']
        value.clear()
        if data['output_format'] == 'informational_carousel':
            value.update(hook=blocks[:1], cards=blocks, conclusion=blocks[-1:])
        else:
            value.update(hook=blocks[:1], spoken_blocks=[{**block,'pause_seconds':0.5} for block in blocks[1:]], visual_support=blocks)
    s = session(seen, mutate_write=formatted)
    request = GenerationRequest(request_id=uuid4(), source=LocalStorage(tmp_path).put_text(SOURCE),
        architecture='literal-v1', audience=audience, output_format=fmt)
    generator = LiteralGenerator(s)
    content = generator.generate(request, extraction.anchors, evidence)
    assert content.output_format.value == fmt and content.audience.value == audience
    assert not format_errors(content)
    from suno_asset.domain.models import EvaluationIssue
    updated = GroqRefiner(generator).refine(content,
        (EvaluationIssue(code='adequacy_failed',message='Ajustar público',correctable=True),),
        extraction.anchors,evidence,1)
    assert updated.output_format == content.output_format and updated.audience == content.audience
    assert updated.revision == 1
    writes = [data for stage, _, data in seen if stage in {'generate','refine'}]
    assert all(data['output_format'] == fmt and data['audience'] == audience for data in writes)
    assert 'previous' in writes[-1]
    previous_texts = {row['text'] for row in writes[-1]['previous']['claims']}
    assert previous_texts <= {claim.text for claim in content.claims}


def test_idempotency_after_default_resolution(tmp_path):
    client = TestClient(create_app(storage=LocalStorage(tmp_path)))
    source = client.post('/documents',json={'text':SOURCE}).json()
    headers = {'Idempotency-Key':'formats-offline-key'}
    first = client.post('/generations',json={'source_id':source['source_id']},headers=headers)
    explicit = client.post('/generations',json={'source_id':source['source_id'], 'audience':'beginner',
        'output_format':'analytical_text','architecture':'current','model_profile':'single'},headers=headers)
    assert explicit.json() == first.json()
    assert client.post('/generations',json={'source_id':source['source_id'], 'architecture':'literal-v1'},headers=headers).status_code == 409


@pytest.mark.parametrize('mismatch', ['audience','output_format'])
def test_workflow_refuses_different_requested_audience_or_format(tmp_path, mismatch):
    from suno_asset.services.fakes import fake_services
    class WrongGenerator:
        def generate(self, request, anchors, evidence):
            request = request.model_copy(update={mismatch: Audience.ADVANCED if mismatch == 'audience' else OutputFormat.ANALYTICAL_TEXT})
            return SimulatedLiteralGenerator().generate(request, anchors, evidence)
    def factory(storage):
        from suno_asset.services.parsing import ManualParser
        return replace(fake_services(storage), parser=ManualParser(), literal_generator=WrongGenerator())
    client = TestClient(create_app(storage=LocalStorage(tmp_path), services_factory=factory))
    source = client.post('/documents',json={'text':SOURCE}).json()
    result = client.post('/generations',json={'source_id':source['source_id'], 'architecture':'literal-v1',
        'output_format':'informational_carousel'}).json()
    assert result['status'] == 'failed' and result['content'] is None
    assert result['errors'][0]['code'] == 'invalid_generation'


def test_real_idempotency_detects_effective_model_change_offline(tmp_path, monkeypatch):
    import importlib
    api_module = importlib.import_module('suno_asset.api.app')
    monkeypatch.setattr(api_module, 'make_session', lambda: session([]))
    client = TestClient(create_app(storage=LocalStorage(tmp_path)))
    source = client.post('/documents',json={'text':SOURCE}).json()
    payload = {'source_id':source['source_id'], 'architecture':'literal-v1', 'mode':'real'}
    headers = {'Idempotency-Key':'real-model-offline-key'}
    monkeypatch.setenv('GROQ_MODEL','openai/gpt-oss-20b')
    first = client.post('/generations',json=payload,headers=headers)
    assert first.status_code == 200
    assert first.json()['manifest']['resolved_configuration']['models']['generate'] == 'openai/gpt-oss-20b'
    monkeypatch.setenv('GROQ_MODEL','openai/gpt-oss-120b')
    assert client.post('/generations',json=payload,headers=headers).status_code == 409


def test_promotion_changes_implicit_defaults_and_respects_explicit_selection(tmp_path, monkeypatch):
    from suno_asset.config import Settings
    import suno_asset.m7_promotion as promotion
    winner = {'id':'literal-quality','architecture':'literal-v1','profile':'quality','models':{}}
    monkeypatch.setattr(promotion,'load_promoted_configuration',lambda _:winner)
    client = TestClient(create_app(storage=LocalStorage(tmp_path),settings=Settings(promoted_configuration_path=tmp_path/'promotion.json')))
    assert client.get('/capabilities').json()['defaults']['architecture'] == 'literal-v1'
    source = client.post('/documents',json={'text':SOURCE}).json()
    implicit = client.post('/generations',json={'source_id':source['source_id']}).json()
    assert implicit['architecture'] == 'literal-v1' and implicit['model_profile'] == 'quality'
    explicit = client.post('/generations',json={'source_id':source['source_id'],'architecture':'current','model_profile':'single'}).json()
    assert explicit['architecture'] == 'current' and explicit['model_profile'] == 'single'


def test_malformed_provider_limit_returns_structured_unavailable(tmp_path, monkeypatch):
    import importlib
    api_module = importlib.import_module('suno_asset.api.app')
    def prohibit_session():
        pytest.fail('Configuração inválida precisa ser recusada antes de criar sessão real.')
    monkeypatch.setattr(api_module,'make_session',prohibit_session)
    monkeypatch.setenv('GROQ_TPM_LIMIT','malformed')
    client = TestClient(create_app(storage=LocalStorage(tmp_path)))
    source = client.post('/documents',json={'text':SOURCE}).json()
    response = client.post('/generations',json={'source_id':source['source_id'], 'mode':'real',
        'architecture':'literal-v1'},headers={'Idempotency-Key':'malformed-limits-key'})
    assert response.status_code == 503
    assert response.json()['detail']['error'] == 'groq_config_invalid'


def test_explicit_selection_works_with_invalid_promotion_and_implicit_fails_closed(tmp_path):
    from suno_asset.config import Settings
    promoted_file = tmp_path/'invalid-promotion.json'
    promoted_file.write_text('{broken-json')
    client = TestClient(create_app(storage=LocalStorage(tmp_path/'storage'),
        settings=Settings(promoted_configuration_path=promoted_file)))
    source = client.post('/documents',json={'text':SOURCE}).json()
    explicit = client.post('/generations',json={'source_id':source['source_id'],
        'architecture':'current','model_profile':'single'})
    assert explicit.status_code == 200 and explicit.json()['architecture'] == 'current'
    implicit = client.post('/generations',json={'source_id':source['source_id']})
    assert implicit.status_code == 503
    assert implicit.json()['detail']['error'] == 'promoted_configuration_invalid'
    capabilities = client.get('/capabilities')
    assert capabilities.status_code == 503
    assert capabilities.json()['detail']['error'] == 'promoted_configuration_invalid'


@pytest.mark.parametrize('fmt', ['informational_carousel','short_video_script'])
@pytest.mark.parametrize('audience', ['beginner','advanced'])
def test_four_writer_batches_share_global_format_limits_without_omission(tmp_path, monkeypatch, fmt, audience):
    import suno_asset.services.literal as literal_module
    source = prepared('\n'.join(f'{i}. A Selic permaneceu elevada.' for i in range(1,9)))
    extraction = SimulatedLiteralExtractor().extract_prepared(source)
    _, evidence = source_artifacts(source)
    packs = []
    def four_batches(_session, _stage, items, build):
        batches = [items[i:i+2] for i in range(0,len(items),2)]
        packs.append([[a.anchor_id for a in batch] for batch in batches])
        # Repacking also evaluates the scoped request, not an unbudgeted proxy.
        for batch in batches:
            build(batch)
        return batches
    monkeypatch.setattr(literal_module,'_pack',four_batches)
    seen = []
    def formatted(value,data):
        blocks = value['development']
        budget = data['batch_budget']
        value.clear()
        hook = blocks[:1] if budget['hook_scope'] == 'generate_here' else []
        if fmt == 'informational_carousel':
            conclusion = blocks[-1:] if budget['conclusion_scope'] == 'generate_here' else []
            value.update(hook=hook,cards=blocks,conclusion=conclusion)
        else:
            # Every source fact remains spoken or in the first hook.
            spoken = blocks[1:] if hook else blocks
            value.update(hook=hook,spoken_blocks=[{**block,'pause_seconds':0.25} for block in spoken],visual_support=blocks)
    s = session(seen,mutate_write=formatted)
    request = GenerationRequest(request_id=uuid4(),source=LocalStorage(tmp_path).put_text(SOURCE),
        architecture='literal-v1',audience=audience,output_format=fmt)
    content = LiteralGenerator(s).generate(request,extraction.anchors,evidence)
    writes = [data for stage, _, data in seen if stage == 'generate']
    assert len(writes) == 4 and len(packs) >= 2
    budgets = [data['batch_budget'] for data in writes]
    assert sum(budget['max_cards'] for budget in budgets) == 10
    assert sum(budget['max_spoken_words_including_education'] for budget in budgets) == 130
    assert sum(budget['max_duration_seconds'] for budget in budgets) == pytest.approx(60)
    educational = sum(b['educational_word_reserve'] for b in budgets)
    assert (educational > 0) == (audience == 'beginner')
    assert sum(b['max_material_spoken_words'] for b in budgets) == 130 - educational
    assert [b['batch_index'] for b in budgets] == [1,2,3,4]
    assert all(b['batch_count'] == 4 and b['scope'] == 'whole_document' for b in budgets)
    assert [b['hook_scope'] for b in budgets] == ['generate_here',*['reuse_first_batch_without_repeating']*3]
    assert [b['conclusion_scope'] for b in budgets] == [*['reuse_last_batch_without_repeating']*3,'generate_here']
    assert {aid for claim in content.claims for aid in claim.anchor_ids} == {a.anchor_id for a in extraction.anchors}
    assert not format_errors(content)
    if fmt == 'informational_carousel':
        assert len(content.cards) == 8
    else:
        assert content.estimated_duration_seconds <= 60
    assert all('DOCUMEN' in data['format_instruction'] or 'INTEIRO' in data['format_instruction'] for data in writes)
