"""API local síncrona do vertical slice simulado."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from hashlib import sha256
import json
import os
import time
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from suno_asset.config import Settings
from suno_asset.domain.models import Architecture, ModelProfile, Audience, ExecutionMode, ExecutionResult, GenerationRequest, OutputFormat
from suno_asset.services.groq import (GroqEvaluator, GroqExtractor, GroqFactVerifier, GroqGenerator, GroqRefiner, GroqSession, make_session, DEADLINE_SECONDS)
from suno_asset.services.provider_config import make_economy_session, provider_identity
from suno_asset.services.deterministic import DeterministicEvaluator, DeterministicFactVerifier, DeterministicGenerator, DeterministicRefiner
from suno_asset.services.local_storage import LocalStorage, MAX_PDF_BYTES, MAX_TEXT_BYTES
from suno_asset.services.parsing import ManualParser
from suno_asset.services.protocols import ExpectedServiceError, Services
from suno_asset.services.retrieval import DeterministicExtractor, LexicalRetriever, FullSourceRetriever
from suno_asset.workflow.graph import LITERAL_POLICY, REAL_POLICY, SIMULATED_POLICY, run_workflow


class TextUpload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(examples=['O Copom decidiu reduzir a Selic para 13,75% ao ano.'])


class GenerationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: UUID = Field(examples=['00000000-0000-0000-0000-000000000001'])
    audience: Audience = Audience.BEGINNER
    output_format: OutputFormat = OutputFormat.ANALYTICAL_TEXT
    editorial_focus: str | None = None
    max_refinements: int = Field(default=2, ge=0, le=2)
    mode: ExecutionMode = ExecutionMode.SIMULATED
    architecture: Architecture | None = None
    model_profile: ModelProfile | None = None


def _error(exc: ExpectedServiceError) -> HTTPException:
    code = 413 if exc.code in {'size_limit', 'text_limit', 'page_limit'} else 400
    if exc.code == 'artifact_missing':
        code = 404
    elif exc.code in {'provider_config_invalid', 'gemini_config_invalid', 'gemini_unavailable', 'gemini_model_unavailable', 'hybrid_preparation_required', 'groq_config_invalid', 'artifact_unavailable', 'generation_unavailable', 'execution_capacity_exhausted', 'storage_busy', 'storage_unavailable', 'execution_interrupted', 'execution_deadline'}:
        code = 503
    elif exc.code == 'gemini_rate_limit':
        code = 429
    elif exc.code in {'resource_not_found'}:
        code = 404
    elif exc.code == 'resource_deleted':
        code = 410
    elif exc.code in {'idempotency_conflict', 'resource_busy', 'execution_identity_conflict'}:
        code = 409
    return HTTPException(status_code=code, detail={'error': exc.code, 'message': str(exc)})


def _real_services(store: LocalStorage, session: GroqSession) -> Services:
    from suno_asset.services.literal import LiteralExtractor, LiteralFactVerifier, LiteralGenerator, LiteralEvaluator
    generator = GroqGenerator(session)
    literal_generator = LiteralGenerator(session)
    literal_evaluator = LiteralEvaluator(session)
    literal_refiner = GroqRefiner(literal_generator)
    if session.provider == 'gemini':
        from suno_asset.services.hybrid_content import HybridGenerator, HybridEvaluator
        literal_generator = HybridGenerator(session)
        literal_evaluator = HybridEvaluator(session)
        literal_refiner = literal_generator
    return Services(storage=store, parser=ManualParser(), retriever=FullSourceRetriever(),
        extractor=GroqExtractor(session), generator=generator, refiner=GroqRefiner(generator),
        evaluator=GroqEvaluator(session), fact_verifier=GroqFactVerifier(session),
        llm_session=session, literal_extractor=LiteralExtractor(session), literal_verifier=LiteralFactVerifier(session),
        literal_generator=literal_generator,literal_refiner=literal_refiner,literal_evaluator=literal_evaluator)


def create_app(
    settings: Settings | None = None, storage: LocalStorage | None = None,
    services_factory: Callable[[LocalStorage], Services] | None = None,
) -> FastAPI:
    config = settings or Settings()
    store = storage or LocalStorage(config.data_root)
    from suno_asset.services.artifact_lifecycle import reconcile
    reconcile(store)
    app = FastAPI(title=config.app_name)
    app.state.storage = store
    repository = store.repository

    @app.get('/health')
    def health() -> dict[str, str]:
        return {'status': 'ok'}

    def promoted_configuration():
        if config.promoted_configuration_path is None:
            return None
        from suno_asset.m7_promotion import load_promoted_configuration
        try:
            return load_promoted_configuration(config.promoted_configuration_path)
        except (ValueError, KeyError, TypeError, OSError, ExpectedServiceError) as exc:
            raise HTTPException(503, detail={'error':'promoted_configuration_invalid', 'message':str(exc)}) from exc

    def mvp_release():
        if config.mvp_release_path is None:
            return None
        from suno_asset.m7_mvp_release import load_mvp_release
        try:
            release=load_mvp_release(config.mvp_release_path)
            if release['version']=='m7-functional-mvp-release-v1' and provider_identity('economy')['provider']=='gemini':
                raise ValueError('aceite Groq histórico não certifica a configuração Gemini')
            return release
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(503, detail={'error':'mvp_release_invalid', 'message':str(exc)}) from exc

    def resolve_configuration(payload: GenerationInput):
        architecture = payload.architecture
        profile = payload.model_profile
        # Explicit choices are reproducible even if the promoted artifact is stale.
        if architecture is None and profile is None:
            release = mvp_release()
            promoted = promoted_configuration() if not release else None
            if release:
                architecture = Architecture.LITERAL
                profile = ModelProfile.ECONOMY
            elif promoted:
                try:
                    architecture = Architecture(promoted['architecture'])
                    profile = ModelProfile(promoted.get('profile', promoted.get('model_profile')))
                except (ValueError, KeyError, TypeError) as exc:
                    raise HTTPException(503, detail={'error':'promoted_configuration_invalid', 'message':str(exc)}) from exc
        if profile is None and architecture == Architecture.LITERAL and mvp_release():
            profile = ModelProfile.ECONOMY
        resolved_profile=profile or ModelProfile.SINGLE
        updates={'architecture':architecture or Architecture.CURRENT,'model_profile':resolved_profile}
        if resolved_profile == ModelProfile.ECONOMY and payload.mode == ExecutionMode.REAL:
            provider = provider_identity('economy')['provider']
            updates['max_refinements']=min(payload.max_refinements,2 if provider=='gemini' else 1)
        return payload.model_copy(update=updates)

    def effective_configuration(payload):
        from suno_asset.services.groq import MODEL, PROMPT_VERSION, LITERAL_PROMPT_VERSION, STAGE_OUTPUT
        identity = provider_identity(payload.model_profile.value) if payload.mode == ExecutionMode.REAL else {
            'provider': None, 'models': {}, 'provider_limits': None}
        from suno_asset.mvp_economy import economy_policy
        policy = (economy_policy(identity['provider']) if payload.model_profile == ModelProfile.ECONOMY else
            LITERAL_POLICY if payload.architecture == Architecture.LITERAL else REAL_POLICY) if payload.mode == ExecutionMode.REAL else SIMULATED_POLICY
        return {'architecture':payload.architecture.value,'model_profile':payload.model_profile.value,
            'audience':payload.audience.value,'output_format':payload.output_format.value,'mode':payload.mode.value,
            **identity, 'prompt_version':LITERAL_PROMPT_VERSION if payload.architecture == Architecture.LITERAL else PROMPT_VERSION,
            'policy_version':policy.version,'content_contract_version':'content-formats-v1',
            }

    @app.get('/capabilities')
    def capabilities():
        try:
            economy = provider_identity('economy')
        except ExpectedServiceError as exc:
            raise _error(exc) from exc
        release = mvp_release()
        promoted = promoted_configuration() if not release else None
        default_arch = Architecture.LITERAL.value if release else promoted['architecture'] if promoted else Architecture.CURRENT.value
        default_profile = ModelProfile.ECONOMY.value if release else promoted.get('profile', promoted.get('model_profile')) if promoted else ModelProfile.SINGLE.value
        combinations = [{'audience':audience.value, 'output_format':fmt.value}
            for audience in Audience for fmt in OutputFormat]
        return {'contract_version':'generation-capabilities-v1',
            'defaults':{'audience':Audience.BEGINNER.value,'output_format':OutputFormat.ANALYTICAL_TEXT.value,
                'architecture':default_arch,'model_profile':default_profile,'mode':ExecutionMode.SIMULATED.value},
            'architectures':{
                Architecture.LITERAL.value:{'modes':[mode.value for mode in ExecutionMode], 'combinations':combinations},
                Architecture.CURRENT.value:{'modes':[mode.value for mode in ExecutionMode], 'combinations':[
                    {'audience':Audience.BEGINNER.value,'output_format':OutputFormat.ANALYTICAL_TEXT.value}]}},
            'promoted_configuration':promoted,'mvp_release':release,
            'economy_configuration':economy,
            'economy_preparation_ready':True,
            'economy_policy_version':__import__('suno_asset.mvp_economy',fromlist=['economy_policy']).economy_policy(economy['provider']).version,
            'economy_call_targets':{'fresh':4,'approved_fact_cache':2,'idempotent_replay':0},
            'acceptance_scope':'functional_mvp' if release else None}

    @app.post('/documents', status_code=201, summary='Registrar PDF ou texto colado',
              description='Envie application/pdf com X-Filename ou application/json com {text}. Apenas um meio por solicitação.',
              openapi_extra={'requestBody': {'required': True, 'content': {
                  'application/pdf': {'schema': {'type': 'string', 'format': 'binary'}},
                  'application/json': {'schema': TextUpload.model_json_schema()},
              }}})
    async def create_document(request: Request):
        media = request.headers.get('content-type', '').split(';', 1)[0].lower()
        if media not in {'application/pdf', 'application/json'}:
            raise HTTPException(415, detail={'error': 'unsupported_media_type'})
        limit = MAX_PDF_BYTES if media == 'application/pdf' else MAX_TEXT_BYTES + 4096
        body = bytearray()
        async for block in request.stream():
            body.extend(block)
            if len(body) > limit:
                raise HTTPException(413, detail={'error': 'size_limit'})
        try:
            if media == 'application/pdf':
                filename = request.headers.get('x-filename')
                if not filename:
                    raise HTTPException(422, detail={'error': 'filename_required'})
                source = store.put_pdf(bytes(body), filename=filename)
            else:
                source = store.put_text(TextUpload.model_validate_json(bytes(body)).text)
        except ExpectedServiceError as exc:
            raise _error(exc) from exc
        except ValidationError as exc:
            raise HTTPException(422, detail={'error': 'invalid_json', 'message': str(exc)}) from exc
        return source.model_dump(mode='json')

    @app.post('/generations', response_model=ExecutionResult)
    def create_generation(payload: GenerationInput, request: Request) -> ExecutionResult:
        payload = resolve_configuration(payload)
        if payload.architecture == Architecture.CURRENT and (payload.audience != Audience.BEGINNER or payload.output_format != OutputFormat.ANALYTICAL_TEXT):
            raise HTTPException(422, detail={'error': 'unsupported_capability'})
        try:
            source = store.load_source(payload.source_id)
        except ExpectedServiceError as exc:
            raise _error(exc) from exc
        try:
            resolved = effective_configuration(payload)
            from suno_asset.services.artifact_lifecycle import runtime_fingerprint
            resolved['execution_code_sha256'] = runtime_fingerprint()
            idempotency_key = request.headers.get('Idempotency-Key')
            if payload.mode == ExecutionMode.REAL or idempotency_key is not None:
                if not idempotency_key or not (8 <= len(idempotency_key) <= 128) or not idempotency_key.isascii() or not idempotency_key.isprintable():
                    raise HTTPException(422, detail={'error': 'idempotency_key_required'})
            fingerprint = sha256(json.dumps({'request':payload.model_dump(mode='json'),'resolved_configuration':resolved}, sort_keys=True).encode()).hexdigest()
            row, owner = repository.claim(str(payload.source_id), fingerprint, payload.mode.value,
                                          key=idempotency_key, configuration=resolved)
        except ExpectedServiceError as exc:
            raise _error(exc) from exc
        request_id = UUID(row['request_id'])
        if not owner:
            while row['state'] in {'registered', 'running'} and time.time() < row['deadline_at'] + 5:
                time.sleep(.05)
                repository.reconcile_owners()
                row = repository.get_run(str(request_id))
            if row['state'] == 'completed':
                return get_generation(request_id)
            status_code = 410 if row['state'] in {'deleting', 'deleted'} else row['http_status'] or 503
            raise HTTPException(status_code, detail={'error': row['error_code'] or 'generation_in_progress',
                                                     'request_id': str(request_id)})
        session = None
        quota_job_id = None
        try:
            if payload.mode == ExecutionMode.REAL:
                session = make_economy_session() if payload.model_profile == ModelProfile.ECONOMY else make_session()
                if session.provider=='gemini':
                    session.max_calls=min(session.max_calls,14)
                if payload.model_profile == ModelProfile.ECONOMY and payload.architecture != Architecture.LITERAL:
                    raise ExpectedServiceError('groq_profile_invalid','perfil economy exige arquitetura literal-v1')
                if payload.model_profile == ModelProfile.ECONOMY:
                    if session.provider != 'gemini':
                        from suno_asset.experiments.literal_efficiency.pipeline import install_nominal_execution_cap
                        install_nominal_execution_cap(session)
                    session.stage_cost_limits={'extract':.06,'verify':.05,'generate':.03,'evaluate':.06,'refine':.06}
                if session.free_only:
                    quota_job_id=f'api-{request_id.hex}'
                    session.quota_job_id=quota_job_id
                    session.quota_ledger.register_job(quota_job_id,fingerprint,str(payload.source_id),
                        json.dumps(resolved,sort_keys=True,separators=(',',':')))
                    session.quota_ledger.start_job(quota_job_id)
            generation_request = GenerationRequest(
                request_id=request_id, source=source, audience=payload.audience,
                output_format=payload.output_format, editorial_focus=payload.editorial_focus,
                max_refinements=payload.max_refinements, mode=payload.mode,
                architecture=payload.architecture,model_profile=payload.model_profile,resolved_configuration=resolved,
            )
            default_services = _real_services(store, session) if session else Services(
                storage=store, parser=ManualParser(), retriever=LexicalRetriever(),
                extractor=DeterministicExtractor(), generator=DeterministicGenerator(),
                refiner=DeterministicRefiner(), evaluator=DeterministicEvaluator(),
                fact_verifier=DeterministicFactVerifier(),
            )
            services = services_factory(store) if services_factory else default_services
            from suno_asset.mvp_economy import economy_policy
            policy = (economy_policy(session.provider) if payload.model_profile == ModelProfile.ECONOMY else
                LITERAL_POLICY if payload.architecture == Architecture.LITERAL else REAL_POLICY) if payload.mode == ExecutionMode.REAL else SIMULATED_POLICY
            result = run_workflow(generation_request, services=services, policy=policy)
            if quota_job_id:
                result_path=store.root/f'runs/{request_id.hex}-result.json'
                session.quota_ledger.complete_job(quota_job_id,result_path,sha256(result_path.read_bytes()).hexdigest())
            return result
        except ExpectedServiceError as exc:
            if quota_job_id:
                try:
                    summary={'version':'api-free-quota-result-v1','request_id':str(request_id),
                        'source_id':str(payload.source_id),'status':'incomplete',
                        'attempts':getattr(session,'attempt_records',[]),
                        'reported_tokens':sum((a.get('prompt_tokens') or 0)+(a.get('completion_tokens') or 0)
                            for a in getattr(session,'attempt_records',[]) if a.get('usage_known') is True),
                        'unknown_reserved_tokens':sum((a.get('input_estimate') or 0)+(a.get('output_reserved') or 0)
                            for a in getattr(session,'attempt_records',[]) if a.get('usage_known') is not True)}
                    store.save_run_artifact(request_id,'quota-state',summary)
                    quota_path=store.root/f'runs/{request_id.hex}-quota-state.json'
                    session.quota_ledger.incomplete_job(quota_job_id,quota_path,sha256(quota_path.read_bytes()).hexdigest())
                except Exception:
                    pass
            error = _error(exc)
            repository.fail_run(str(request_id), exc.code, error.status_code)
            raise HTTPException(error.status_code, detail={'error': exc.code, 'request_id': str(request_id)}) from exc
        except Exception:
            if quota_job_id:
                try:
                    summary={'version':'api-free-quota-result-v1','request_id':str(request_id),
                        'source_id':str(payload.source_id),'status':'incomplete',
                        'attempts':getattr(session,'attempt_records',[]),
                        'reported_tokens':sum((a.get('prompt_tokens') or 0)+(a.get('completion_tokens') or 0)
                            for a in getattr(session,'attempt_records',[]) if a.get('usage_known') is True),
                        'unknown_reserved_tokens':sum((a.get('input_estimate') or 0)+(a.get('output_reserved') or 0)
                            for a in getattr(session,'attempt_records',[]) if a.get('usage_known') is not True)}
                    store.save_run_artifact(request_id,'quota-state',summary)
                    quota_path=store.root/f'runs/{request_id.hex}-quota-state.json'
                    session.quota_ledger.incomplete_job(quota_job_id,quota_path,sha256(quota_path.read_bytes()).hexdigest())
                except Exception:
                    pass
            repository.fail_run(str(request_id), 'generation_unavailable', 500)
            raise HTTPException(500, detail={'error': 'generation_unavailable', 'request_id': str(request_id)}) from None

    @app.get('/generations/{request_id}', response_model=ExecutionResult)
    def get_generation(request_id: UUID) -> ExecutionResult:
        try:
            return store.load_execution_result(request_id)
        except ExpectedServiceError as exc:
            raise _error(exc) from exc

    @app.get('/generations')
    def list_generations(limit: int = Query(default=50, ge=1, le=100)):
        """Return compact summaries for the local studio's recent-run view."""
        repository.reconcile_owners()
        rows = [row for row in reversed(repository.list_runs())
                if row['state'] not in {'deleting', 'deleted'}][:limit]
        items = []
        for row in rows:
            config = row['configuration']
            try:
                source = store.load_source(UUID(row['source_id']))
                name = source.original_filename or ('Texto da ata' if source.origin.value == 'text' else 'Documento da ata')
            except (ExpectedServiceError, ValueError):
                name = 'Fonte indisponível'
            status = 'failed'
            if row['state'] == 'completed':
                try:
                    result = store.load_execution_result(UUID(row['request_id']))
                    status = result.status.value
                    source_text = next((unit.text for unit in (result.prepared_source.units if result.prepared_source else [])
                                        if isinstance(unit.text, str) and unit.text.strip()), None)
                    if source_text:
                        name = next((line.strip()[:80] for line in source_text.splitlines() if line.strip()), name)
                except ExpectedServiceError:
                    status = 'failed'
            elif row['state'] in {'registered', 'running'}:
                status = 'processing'
            elif row['state'] == 'interrupted':
                status = 'failed'
            items.append({'request_id':row['request_id'],'name':name,'date':row['created_at'],
                'status':status,'state':row['state'],'audience':config.get('audience','beginner'),
                'output_format':config.get('output_format','analytical_text'),'mode':row['mode'],
                'source_id':row['source_id']})
        return {'items':items}

    @app.get('/generations/{request_id}/status')
    def get_generation_status(request_id: UUID):
        repository.reconcile_owners()
        row = repository.get_run(str(request_id))
        if row is None:
            raise HTTPException(404, detail={'error': 'generation_missing'})
        if row['state'] in {'deleting', 'deleted'}:
            raise HTTPException(410, detail={'error': 'resource_deleted'})
        if row['state'] == 'completed':
            try:
                store.load_execution_result(request_id)
            except ExpectedServiceError:
                row = repository.get_run(str(request_id))
        events = repository.get_events(str(request_id))
        return {key: row[key] for key in ('request_id', 'state', 'created_at', 'started_at',
                'deadline_at', 'finished_at', 'error_code')} | {
                'node': events[-1]['node'] if events else None}

    @app.get('/generations/{request_id}/evidence/{index}')
    def get_evidence(request_id: UUID, index: int):
        result = get_generation(request_id)
        if index < 0 or index >= len(result.references):
            raise HTTPException(404, detail={'error': 'evidence_missing'})
        try:
            evidence = store.resolve_evidence(result.references[index])
        except ExpectedServiceError as exc:
            raise _error(exc) from exc
        return evidence.model_dump(mode='json')

    # Serve the local studio from the same origin as the API, including in wheels.
    web_root = Path(__file__).resolve().parents[1] / 'web'
    app.mount('/studio', StaticFiles(directory=web_root), name='studio')

    @app.get('/', include_in_schema=False)
    def studio():
        return FileResponse(web_root / 'index.html')

    return app


app = create_app()
