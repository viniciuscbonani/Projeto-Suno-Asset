"""Orquestração determinística, sem I/O implícito ou LLM real."""

from __future__ import annotations

from collections.abc import Callable
import inspect

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from suno_asset.domain.models import (
    Anchor, Architecture, PreparedSource, ExtractionResult, Importance,
    CheckResult,
    Chunk,
    EvaluationPolicy,
    EvaluationReport,
    Evidence,
    EvidenceRef,
    FactVerificationReport,
    ExecutionMode,
    ExecutionResult,
    ExecutionStatus,
    GeneratedContent,
    GenerationRequest,
    LLMCall,
    Revision,
    SourceOrigin,
    WorkflowError,
)
from suno_asset.services.protocols import ExpectedServiceError, Services
from suno_asset.services.content_formats import validate_content, format_errors
from suno_asset.services.ingestion import register_document, parse_document
from suno_asset.workflow.state import WorkflowState


SIMULATED_POLICY = EvaluationPolicy(
    version="simulated-v1", required_checks=("structure", "coverage", "support")
)
REAL_POLICY = EvaluationPolicy(
    version="real-m7-v15", required_checks=("structure", "coverage", "support", "numbers", "citations", "adequacy")
)


LITERAL_POLICY = EvaluationPolicy(
    version="real-literal-v2", required_checks=REAL_POLICY.required_checks
)
from suno_asset.mvp_economy import MVP_POLICY, select_anchors, economy_policy


def _validated(model: type, value: object):
    """Revalidar até instâncias Pydantic retornadas por model_copy(update=...)."""
    if isinstance(value, model):
        value = value.model_dump(mode="python")
    return model.model_validate(value)


def _validated_items(model: type, values: object) -> tuple:
    if not isinstance(values, (list, tuple)):
        raise ExpectedServiceError("invalid_service_output", "serviço retornou coleção inválida")
    return tuple(_validated(model, value) for value in values)


def _selected(history: tuple[Revision, ...], number: int | None) -> Revision | None:
    if number is None:
        return None
    return next((item for item in history if item.number == number and item.content is not None), None)


def _no_regression(previous: EvaluationReport | None, candidate: EvaluationReport) -> bool:
    if previous is None:
        return True
    old_facts = {f.anchor_id for f in previous.critical_facts if f.result == CheckResult.PASSED}
    new_facts = {f.anchor_id for f in candidate.critical_facts if f.result == CheckResult.PASSED}
    if not old_facts <= new_facts:
        return False
    old_hard = {item.code for item in previous.issues if item.hard_failure}
    new_hard = {item.code for item in candidate.issues if item.hard_failure}
    if new_hard - old_hard:
        return False
    old_passed = {
        check.name
        for check in previous.checks
        if check.name in previous.policy.required_checks and check.result == CheckResult.PASSED
    }
    new_passed = {
        check.name
        for check in candidate.checks
        if check.name in candidate.policy.required_checks and check.result == CheckResult.PASSED
    }
    return old_passed <= new_passed


def build_graph(services: Services, policy: EvaluationPolicy = SIMULATED_POLICY):
    """Compila um grafo por conjunto de dependências; serviços nunca entram no State."""

    def expected_failure(
        state: WorkflowState, stage: str, exc: ExpectedServiceError
    ) -> dict:
        error = WorkflowError(stage=stage, code=exc.code, message=str(exc))
        updates: dict = {"errors": (error,), "reason": str(exc)}
        history = state.get("history", ())
        selected_number = state.get("selected_revision")
        draft = state.get("draft")
        if stage == "refine":
            failed_revision = state.get("iteration", 0) + 1
            updates["iteration"] = failed_revision
            deduped=tuple({item.number:item for item in history}.values())
            updates["history"] = (*deduped, Revision(number=failed_revision, error=error))
        elif draft is not None and not any(item.number == draft.revision for item in history):
            updates["history"] = (
                Revision(number=draft.revision, content=draft, error=error),
            )
            if selected_number is None:
                selected_number = draft.revision
                updates["selected_revision"] = selected_number
        elif stage == "generate":
            updates["history"] = (Revision(number=state.get("iteration", 0), error=error),)

        updates["status"] = (
            ExecutionStatus.NEEDS_REVIEW
            if selected_number is not None
            else ExecutionStatus.FAILED
        )
        return updates

    def guard(stage: str, node: Callable[[WorkflowState], dict]):
        def wrapped(state: WorkflowState) -> dict:
            try:
                return node(state)
            except ExpectedServiceError as exc:
                lock=state.get('compact_cache_lock')
                if lock:
                    lock.__exit__(None,None,None)
                return expected_failure(state, stage, exc)
            except ValidationError:
                lock=state.get('compact_cache_lock')
                if lock:
                    lock.__exit__(None,None,None)
                return expected_failure(
                    state,
                    stage,
                    ExpectedServiceError(
                        "invalid_service_output", "serviço retornou contrato inválido"
                    ),
                )

        return wrapped

    def ingest(state: WorkflowState) -> dict:
        document = register_document(services.storage, state["request"].source)
        return {"document": document}

    def parse(state: WorkflowState) -> dict:
        document, parsed = parse_document(
            services.storage, services.parser, state["document"]
        )
        return {
            "document": document,
            "unit_ids": tuple(unit.unit_id for unit in parsed.units),
            "parse_diagnostics": parsed.diagnostics,
        }

    def retrieve(state: WorkflowState) -> dict:
        if state['request'].architecture == Architecture.LITERAL:
            from suno_asset.services.source_preparation import prepare_source, source_artifacts
            units = services.storage.load_units(state['document'].document_id)
            if state['request'].mode==ExecutionMode.REAL and state['request'].model_profile.value=='economy' and getattr(services.llm_session,'provider',None)=='gemini':
                # Preserve the existing logical segmentation. Its local token
                # heuristic is separate from Gemini request admission (bytes).
                # The legacy Groq preparer enforces a 2k block+context batching
                # guard that is inapplicable to the new single-document request.
                source=prepare_source(units)
                services.llm_session.max_calls=min(services.llm_session.max_calls,14)
            else:
                source = (services.literal_extractor.prepare(units) if services.literal_extractor and hasattr(services.literal_extractor,'prepare')
                    else prepare_source(units,getattr(services.llm_session,'counter',None)))
            chunks, evidence = source_artifacts(source)
            if hasattr(services.storage,'save_source_chunks'):
                services.storage.save_source_chunks(source.document_id,chunks)
            else:
                services.storage.save_chunks(source.document_id,chunks)
            services.storage.save_evidence(evidence)
            return {'prepared_source': source, 'chunk_ids': tuple(c.chunk_id for c in chunks),
                'evidence_refs': tuple(e.ref for e in evidence)}
        document = state["document"]
        units = services.storage.load_units(document.document_id)
        chunks_raw, evidence_raw = services.retriever.retrieve(
            document, units, state["request"].editorial_focus
        )
        chunks = _validated_items(Chunk, chunks_raw)
        evidence = _validated_items(Evidence, evidence_raw)
        unit_by_id = {item.unit_id: item for item in units}
        chunk_by_id = {item.chunk_id: item for item in chunks}
        if (
            not chunks or not evidence
            or len(chunk_by_id) != len(chunks)
            or len({item.ref for item in evidence}) != len(evidence)
            or any(
                chunk.document_id != document.document_id
                or chunk.unit_id not in unit_by_id
                or chunk.parser_version != unit_by_id[chunk.unit_id].parser_version
                or chunk.page != unit_by_id[chunk.unit_id].page
                or chunk.start < unit_by_id[chunk.unit_id].start
                or chunk.end > unit_by_id[chunk.unit_id].end
                or unit_by_id[chunk.unit_id].text[
                    chunk.start - unit_by_id[chunk.unit_id].start:
                    chunk.end - unit_by_id[chunk.unit_id].start
                ] != chunk.text
                for chunk in chunks
            )
            or any(
                item.ref.document_id != document.document_id
                or item.ref.chunk_id not in chunk_by_id
                or item.ref.parser_version != chunk_by_id[item.ref.chunk_id].parser_version
                or item.ref.page != chunk_by_id[item.ref.chunk_id].page
                or item.ref.start < chunk_by_id[item.ref.chunk_id].start
                or item.ref.end > chunk_by_id[item.ref.chunk_id].end
                or chunk_by_id[item.ref.chunk_id].text[
                    item.ref.start - chunk_by_id[item.ref.chunk_id].start:
                    item.ref.end - chunk_by_id[item.ref.chunk_id].start
                ] != item.text
                for item in evidence
            )
        ):
            raise ExpectedServiceError("invalid_retrieval", "evidências recuperadas inválidas")
        services.storage.save_chunks(document.document_id, chunks)
        services.storage.save_evidence(evidence)
        return {
            "chunk_ids": tuple(item.chunk_id for item in chunks),
            "evidence_refs": tuple(item.ref for item in evidence),
            "missing_facets": tuple(getattr(services.retriever, "last_missing_facets", ())),
        }

    def resolved(state: WorkflowState) -> tuple[Evidence, ...]:
        document_id = state["document"].document_id
        evidence = tuple(services.storage.resolve_evidence(ref) for ref in state["evidence_refs"])
        if any(
            item.ref != requested or item.ref.document_id != document_id
            for item, requested in zip(evidence, state["evidence_refs"], strict=True)
        ):
            raise ExpectedServiceError("invalid_evidence", "evidência diverge da referência solicitada")
        return _validated_items(Evidence, evidence)

    def extract_anchors(state: WorkflowState) -> dict:
        if state['request'].architecture == Architecture.LITERAL:
            from suno_asset.services.literal import validate_extraction, SimulatedLiteralExtractor
            service = services.literal_extractor or (SimulatedLiteralExtractor() if state['request'].mode == ExecutionMode.SIMULATED else None)
            if service is None: raise ExpectedServiceError('literal_service_missing','extrator literal ausente')
            if state['request'].mode == ExecutionMode.REAL and state['request'].model_profile.value == 'economy':
                if getattr(services.llm_session, 'provider', 'groq') == 'gemini':
                    from suno_asset.services.hybrid_facts import prepare
                    pack,extraction,report = prepare(state['prepared_source'],services.llm_session,
                        state['document'].sha256,services.storage.root)
                    validate_extraction(state['prepared_source'],extraction)
                    return {'anchors':extraction.anchors,'extraction':extraction,'compact_factpack':pack,
                        'compact_verification':report,'compact_cache_hit':services.llm_session.hybrid_cache['hit']}
                from pathlib import Path
                from hashlib import sha256
                import json
                from suno_asset.experiments.literal_efficiency import FACTS_VERSION
                from suno_asset.experiments.literal_efficiency.cache import VerifiedFactCache, cache_key
                from suno_asset.experiments.literal_efficiency.contracts import CompactFactPack, FACT_SCHEMA, adapt_to_legacy
                from suno_asset.experiments.literal_efficiency import contracts as compact_contracts
                from suno_asset.experiments.literal_efficiency.pipeline import EXTRACTOR_VERSION, STATIC_COMPACT_PROMPT, compact_extract
                _legacy_policy=LITERAL_POLICY
                cache = VerifiedFactCache(Path(services.storage.root) / 'mvp-verified-cache')
                source = state['prepared_source']
                context_sha = sha256(json.dumps([e.text for e in source.excerpts],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                deps = {'source_sha256':state['document'].sha256,'block_id':'document',
                    'provider':getattr(services.llm_session,'provider','groq'),
                    'reasoning_effort':'low',
                    'context_sha256':context_sha,'parser_version':source.units[0].parser_version,
                    'segmentation_version':source.segmentation_version,'extractor_version':EXTRACTOR_VERSION,
                    'model_config':json.dumps({stage:services.llm_session.model_for(stage) for stage in
                        ('extract','verify','generate','evaluate','refine')},sort_keys=True)+
                        '|extract-fallback120-terminal-schema-rejection-v1',
                    'prompt_version':sha256(STATIC_COMPACT_PROMPT.encode()).hexdigest(),
                    'schema_version':sha256(__import__('json').dumps(FACT_SCHEMA,sort_keys=True).encode()).hexdigest()+':'+FACTS_VERSION,
                    'verification_policy':MVP_POLICY.version+':'+_legacy_policy.version+':'+services.llm_session.model_for('verify')+
                        ':fallback120-after-terminal-json-schema-rejection-v1'+
                        ':'+sha256((Path(__file__).resolve().parent.parent/'services'/'literal.py').read_bytes()).hexdigest()+
                        ':'+sha256(Path(compact_extract.__code__.co_filename).read_bytes()).hexdigest()+
                        ':'+sha256(Path(compact_contracts.__file__).read_bytes()).hexdigest()}
                key=cache_key(deps)
                lock=cache.lock(key);lock.__enter__()
                try:
                    cached=cache.get(key,deps)
                    if cached:
                        pack=CompactFactPack.model_validate(cached['facts']['factpack'])
                        extraction=adapt_to_legacy(source,pack)
                        validate_extraction(source,extraction)
                        verification=FactVerificationReport.model_validate(cached['facts']['verification_report'])
                        return {'anchors':extraction.anchors,'extraction':extraction,'compact_factpack':pack,
                            'compact_verification':verification,'compact_cache':(cache,key,deps),
                            'compact_cache_lock':lock,'compact_cache_hit':True}
                    pack=compact_extract(source,services.llm_session,variant='B1',source_sha256=state['document'].sha256)
                    extraction=adapt_to_legacy(source,pack)
                    validate_extraction(source,extraction)
                    return {'anchors':extraction.anchors,'extraction':extraction,'compact_factpack':pack,
                        'compact_cache':(cache,key,deps),'compact_cache_lock':lock,'compact_cache_hit':False}
                except Exception:
                    lock.__exit__(None,None,None)
                    raise
            extraction = _validated(ExtractionResult, service.extract_prepared(state['prepared_source']))
            validate_extraction(state['prepared_source'], extraction)
            return {'anchors': extraction.anchors, 'extraction': extraction}
        anchors = _validated_items(Anchor, services.extractor.extract(resolved(state)))
        if not anchors or any(
            ref not in state["evidence_refs"]
            for anchor in anchors
            for ref in anchor.evidence_refs
        ):
            raise ExpectedServiceError("invalid_anchors", "âncoras sem evidência recuperada")
        return {"anchors": anchors}

    def verify_facts(state: WorkflowState) -> dict:
        if state['request'].architecture == Architecture.LITERAL:
            from suno_asset.services.literal import SimulatedLiteralVerifier
            service = services.literal_verifier or (SimulatedLiteralVerifier() if state['request'].mode == ExecutionMode.SIMULATED else None)
            if service is None: raise ExpectedServiceError('literal_service_missing','verificador literal ausente')
            if state.get('compact_verification') is not None:
                report = _validated(FactVerificationReport,state['compact_verification'])
            elif state['request'].mode == ExecutionMode.REAL and state['request'].model_profile.value == 'economy':
                report = _validated(FactVerificationReport, service.verify_prepared(
                    state['prepared_source'],state['extraction'],policy,compact=True))
            else:
                report = _validated(FactVerificationReport, service.verify_prepared(
                    state['prepared_source'],state['extraction'],policy))
            expected = {r.block_id:set(r.anchor_ids) for r in state['extraction'].processing}
            if (report.processing_complete is not True or len(report.block_verifications)!=len(expected)
                or {r.block_id for r in report.block_verifications}!=set(expected)
                or any(len(r.anchors)!=len(expected[r.block_id]) or {a.anchor_id for a in r.anchors}!=expected[r.block_id]
                    for r in report.block_verifications)
                or (report.approved and any(r.support!=CheckResult.PASSED or r.coverage!=CheckResult.PASSED
                    or r.relevance!=CheckResult.PASSED or any(a.result!=CheckResult.PASSED for a in r.anchors)
                    for r in report.block_verifications))):
                raise ExpectedServiceError('invalid_block_verification','verificação por bloco incompleta/inconclusiva')
        else:
            report = _validated(FactVerificationReport, services.fact_verifier.verify(state['anchors'],resolved(state),policy))
        if report.policy_version != policy.version or report.anchor_ids != tuple(
            anchor.anchor_id for anchor in state["anchors"]
        ):
            raise ExpectedServiceError("invalid_fact_verification", "verificação de outros fatos ou política")
        if not report.approved:
            lock=state.get('compact_cache_lock')
            if lock: lock.__exit__(None,None,None)
            return {
                "fact_verification": report,
                "status": ExecutionStatus.FAILED,
                "reason": "fatos necessários não verificáveis antes da geração",
            }
        updates={"fact_verification": report}
        lock=state.get('compact_cache_lock')
        cache_state=state.get('compact_cache')
        if cache_state and state.get('compact_cache_hit') is not True:
            cache,key,deps=cache_state
            cache.put(key,deps,{'factpack':state['compact_factpack'].model_dump(mode='json'),
                'verification_report':report.model_dump(mode='json')},verified=True)
        if lock: lock.__exit__(None,None,None)
        return updates

    def select_editorial_facts(state: WorkflowState) -> dict:
        if (state['request'].mode != ExecutionMode.REAL or state['request'].model_profile.value != 'economy'
            or state.get('compact_factpack') is None):
            return {}
        try:
            if getattr(services.llm_session,'provider','groq') == 'gemini':
                from suno_asset.services.hybrid_content import select
                try:
                    selected,manifest=select(state['prepared_source'],state['compact_factpack'],state['anchors'],state['request'].output_format.value)
                except ExpectedServiceError as exc:
                    return {'status':ExecutionStatus.NEEDS_REVIEW,'reason':str(exc),
                        'errors':(WorkflowError(stage='select_editorial_facts',code=exc.code,message=str(exc)),)}
            else:
                selected,manifest=select_anchors(state['compact_factpack'],state['anchors'],
                    state['request'].output_format.value,state['prepared_source'])
        except ValueError as exc:
            raise ExpectedServiceError('editorial_selection_pending',str(exc)) from exc
        manifest.update({'cache_hit':bool(state.get('compact_cache_hit')),
            'models':services.llm_session.manifest().get('models'), 'policy_version':policy.version})
        return {'anchors':selected,'editorial_selection':manifest}

    def generate(state: WorkflowState) -> dict:
        generator = services.generator
        if state['request'].architecture == Architecture.LITERAL:
            from suno_asset.services.literal import SimulatedLiteralGenerator
            generator = services.literal_generator or (SimulatedLiteralGenerator() if state['request'].mode == ExecutionMode.SIMULATED else None)
            if generator is None: raise ExpectedServiceError('literal_service_missing','gerador literal ausente')
        content = validate_content(generator.generate(state['request'],state['anchors'],resolved(state)))
        if content.revision != 0 or content.audience != state["request"].audience or content.output_format != state["request"].output_format:
            raise ExpectedServiceError("invalid_generation", "rascunho inicial divergente")
        return {"draft": content}

    def evaluate(state: WorkflowState) -> dict:
        draft = state["draft"]
        evaluator = services.evaluator
        if state['request'].architecture == Architecture.LITERAL:
            from suno_asset.services.literal import SimulatedLiteralEvaluator
            evaluator = services.literal_evaluator or (SimulatedLiteralEvaluator() if state['request'].mode == ExecutionMode.SIMULATED else None)
            if evaluator is None: raise ExpectedServiceError('literal_service_missing','avaliador literal ausente')
        report = _validated(EvaluationReport,evaluator.evaluate(draft,state['anchors'],resolved(state),policy))
        if state['request'].architecture == Architecture.LITERAL:
            expected = {a.anchor_id for a in state['anchors']}
            if len(report.critical_facts)!=len(expected) or {f.anchor_id for f in report.critical_facts}!=expected:
                raise ExpectedServiceError('invalid_critical_verification','preservação dos fatos críticos incompleta')
            if report.approved and any(f.result != CheckResult.PASSED for f in report.critical_facts):
                raise ExpectedServiceError('invalid_critical_verification','aprovação com fatos críticos não preservados')
        if report.revision != draft.revision or report.policy != policy:
            raise ExpectedServiceError("invalid_evaluation", "avaliação de outra revisão ou política")
        problems = format_errors(draft,economy=state['request'].mode==ExecutionMode.REAL and state['request'].model_profile.value=='economy')
        if problems:
            existing = {issue.code for issue in report.issues}
            report = report.model_copy(update={'approved': False, 'issues': (*report.issues, *(issue for issue in problems if issue.code not in existing))})
        candidate = Revision(number=draft.revision, content=draft, evaluation=report)
        previous = _selected(state.get("history", ()), state.get("selected_revision"))
        select = previous is None or _no_regression(previous.evaluation, report)
        updates: dict = {"evaluation": report, "history": (candidate,)}
        if select:
            updates["selected_revision"] = draft.revision
        deferred_format_only = (
            report.deferred_due_to_format
            and {issue.code for issue in report.issues} == {'format_noncompliant'}
            and not state.get('errors')
        )
        can_refine = state["iteration"] < state["max_refinements"] and (
            (any(issue.correctable for issue in report.issues) and all(
                check.result != CheckResult.NOT_VERIFIED
                for check in report.checks if check.name in policy.required_checks
            )) or deferred_format_only
        )
        if report.approved and select:
            updates.update(status=ExecutionStatus.APPROVED, reason="verificações da política aprovadas")
        elif report.approved and not select:
            updates.update(status=ExecutionStatus.NEEDS_REVIEW, reason="refinamento rejeitado por regressão de fatos críticos")
        elif can_refine:
            updates.update(status=ExecutionStatus.RUNNING, reason="refinamento permitido")
        else:
            updates.update(
                status=ExecutionStatus.NEEDS_REVIEW,
                reason="avaliação não aprovada ou limite de refinamento atingido",
            )
        return updates

    def refine(state: WorkflowState) -> dict:
        next_revision = state["iteration"] + 1
        refiner = services.literal_refiner if state['request'].architecture == Architecture.LITERAL else services.refiner
        if refiner is None and state['request'].architecture == Architecture.LITERAL and state['request'].mode == ExecutionMode.SIMULATED:
            from suno_asset.services.literal import SimulatedLiteralGenerator
            refiner = SimulatedLiteralGenerator()
        refiner = refiner or services.refiner
        refine_args = (
            state["draft"], state["evaluation"].issues,
            state["anchors"], resolved(state), next_revision,
        )
        refine_parameters = inspect.signature(refiner.refine).parameters
        refine_kwargs = ({'editorial_focus': state['request'].editorial_focus}
                         if 'editorial_focus' in refine_parameters else {})
        content = validate_content(refiner.refine(*refine_args, **refine_kwargs))
        if content.revision != next_revision or content.audience != state["request"].audience or content.output_format != state["request"].output_format:
            raise ExpectedServiceError("invalid_refinement", "revisão refinada divergente")
        return {"draft": content, "iteration": next_revision}

    def finalize(state: WorkflowState) -> dict:
        status = state.get("status", ExecutionStatus.FAILED)
        history = tuple({item.number:item for item in state.get("history", ())}.values())
        selected = _selected(history, state.get("selected_revision"))
        if selected is None:
            status = ExecutionStatus.FAILED
        document = state.get("document")
        result = ExecutionResult(
            request_id=state["request"].request_id,
            architecture=state['request'].architecture, model_profile=state['request'].model_profile,
            prepared_source=state.get('prepared_source'), extraction=state.get('extraction'),
            manifest={**(services.llm_session.manifest() if services.llm_session and hasattr(services.llm_session,'manifest') else
                {'version':'execution-manifest-v2','architecture':state['request'].architecture.value,
                    'profile':state['request'].model_profile.value,'models':{},'mode':state['request'].mode.value}),
                'policy_version':policy.version,'contract_version':'literal-workflow-v1' if state.get('prepared_source') else 'legacy-v1',
                'segmentation_version':state['prepared_source'].segmentation_version if state.get('prepared_source') else None,
                'source_target_tokens':state['prepared_source'].target_source_tokens if state.get('prepared_source') else None,
                'source_max_tokens':state['prepared_source'].max_source_tokens if state.get('prepared_source') else None,
                'resolved_configuration': {**state['request'].resolved_configuration, 'architecture':state['request'].architecture.value, 'model_profile':state['request'].model_profile.value,
                    'audience':state['request'].audience.value,'output_format':state['request'].output_format.value,'mode':state['request'].mode.value}},
            document_id=document.document_id if document else None,
            mode=state["request"].mode,
            status=status,
            reason=state.get("reason", "execução encerrada"),
            policy_version=policy.version,
            selected_revision=selected.number if selected else None,
            fact_verification=state.get("fact_verification"),
            missing_facets=state.get("missing_facets", ()),
            parse_diagnostics=state.get("parse_diagnostics", ()),
            content=selected.content if selected else None,
            evaluation=selected.evaluation if selected else None,
            references=selected.content.references if selected and selected.content else (),
            history=history,
            errors=state.get("errors", ()),
            llm_calls=tuple(LLMCall.model_validate(item) for item in getattr(services.llm_session, "records", ())),
            llm_attempts=getattr(services.llm_session, "calls", 0),
            llm_reserved_tokens=getattr(services.llm_session, "reserved_tokens", 0),
        )
        if state.get('editorial_selection'):
            result=result.model_copy(update={'manifest':{**result.manifest,'mvp_efficient_v1':state['editorial_selection']}})
        if getattr(services.llm_session,'provider',None)=='gemini':
            result=result.model_copy(update={'manifest':{**result.manifest,
                'hybrid':{'version':'hybrid-facts-v2','coverage_scope':'complete_source_factpack_then_selected_facts',
                    'source_sha256':getattr(getattr(services.llm_session,'hybrid_pack',None),'source_sha256',None),
                    'cache':getattr(services.llm_session,'hybrid_cache',None),
                    'selection':state.get('editorial_selection'),
                    'call_targets':{'fresh':4,'cached':2,'replay':0},
                    'attempt_limit':services.llm_session.max_calls}}})
        if hasattr(services.storage, 'save_run_artifact'):
            if state.get('prepared_source'):
                services.storage.save_run_artifact(result.request_id,'prepared-source',state['prepared_source'].model_dump(mode='json'))
            if state.get('extraction'):
                services.storage.save_run_artifact(result.request_id,'extraction',state['extraction'].model_dump(mode='json'))
            if getattr(services.llm_session,'hybrid_pack',None) is not None:
                services.storage.save_run_artifact(result.request_id,'hybrid-factpack',services.llm_session.hybrid_pack.model_dump(mode='json'))
            if services.llm_session:
                services.storage.save_run_artifact(result.request_id,'llm-trace',{
                    'manifest':result.manifest,'plans':getattr(services.llm_session,'plans',[]),
                    'attempts':getattr(services.llm_session,'attempt_records',[]),
                    'corrections':getattr(services.llm_session,'extraction_corrections',[]),
                    'events':getattr(services.llm_session,'literal_artifacts',[])})
            services.storage.save_run_artifact(result.request_id, 'result', result.model_dump(mode='json'))
            if hasattr(services.storage, 'commit_execution'):
                services.storage.commit_execution(result)
        return {"status": status, "result": result}

    from suno_asset.services.observability import instrument
    graph = StateGraph(WorkflowState)
    nodes = {
        "ingest": ingest,
        "parse": parse,
        "retrieve": retrieve,
        "extract_anchors": extract_anchors,
        "verify_facts": verify_facts,
        "select_editorial_facts": select_editorial_facts,
        "generate": generate,
        "evaluate": evaluate,
        "refine": refine,
    }
    for name, node in nodes.items():
        graph.add_node(name, instrument(services.storage, name, guard(name, node)))
    graph.add_node("finalize", instrument(services.storage, "finalize", finalize))
    graph.add_edge(START, "ingest")
    for current, following in (
        ("ingest", "parse"),
        ("parse", "retrieve"),
        ("retrieve", "extract_anchors"),
        ("extract_anchors", "verify_facts"),
        ("generate", "evaluate"),
        ("refine", "evaluate"),
    ):
        graph.add_conditional_edges(
            current,
            lambda state, next_node=following: (
                "finalize" if state["status"] != ExecutionStatus.RUNNING else next_node
            ),
            {"finalize": "finalize", following: following},
        )
    graph.add_conditional_edges("verify_facts",
        lambda state: "finalize" if state["status"] != ExecutionStatus.RUNNING else "select_editorial_facts",
        {"finalize":"finalize","select_editorial_facts":"select_editorial_facts"})
    graph.add_conditional_edges("select_editorial_facts",
        lambda state: "generate" if state["status"] == ExecutionStatus.RUNNING else "finalize",
        {"generate":"generate","finalize":"finalize"})
    graph.add_conditional_edges(
        "evaluate",
        lambda state: (
            "refine" if state["status"] == ExecutionStatus.RUNNING else "finalize"
        ),
        {"refine": "refine", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    return graph.compile()


def run_workflow(
    request: GenerationRequest,
    *,
    services: Services,
    policy: EvaluationPolicy = SIMULATED_POLICY,
) -> ExecutionResult:
    request = _validated(GenerationRequest, request)
    if (
        request.mode not in (ExecutionMode.SIMULATED, ExecutionMode.REAL)
        or request.source.origin not in (SourceOrigin.UPLOAD, SourceOrigin.TEXT)
        or (request.architecture == Architecture.CURRENT and (request.audience.value != "beginner"
            or request.output_format.value != "analytical_text"))
    ):
        raise ValueError("capacidade ainda não implementada")
    expected_policy = LITERAL_POLICY if request.architecture == Architecture.LITERAL else REAL_POLICY
    if request.mode == ExecutionMode.REAL and (policy != (economy_policy(getattr(services.llm_session,'provider','groq')) if request.model_profile.value == 'economy' else expected_policy) or services.llm_session is None):
        raise ValueError("serviços ou política reais não configurados")
    if hasattr(services.storage, 'begin_execution'):
        from suno_asset.services.artifact_lifecycle import runtime_fingerprint
        request = request.model_copy(update={'resolved_configuration': {**request.resolved_configuration,
            'execution_code_sha256': runtime_fingerprint()}})
        restored = services.storage.begin_execution(request)
        if restored is not None:
            return _validated(ExecutionResult, restored)
        if services.llm_session and hasattr(services.llm_session, 'transport'):
            from suno_asset.services.observability import attach_provider_audit
            attach_provider_audit(services.storage, services.llm_session, request.request_id)
            import time
            row = services.storage.repository.get_run(str(request.request_id))
            services.llm_session.deadline_seconds = min(services.llm_session.deadline_seconds,
                time.monotonic() - services.llm_session.started + max(0, row['deadline_at'] - time.time()))
    try:
        if services.llm_session and hasattr(services.llm_session, 'configure'):
            services.llm_session.configure(request.architecture.value, request.model_profile.value)
        final = build_graph(services, policy).invoke(
            {
                "request": request,
                "iteration": 0,
                "max_refinements": request.max_refinements,
                "selected_revision": None,
                "status": ExecutionStatus.RUNNING,
            },
            config={"recursion_limit": 25},
        )
        return _validated(ExecutionResult, final["result"])
    except Exception as exc:
        repository = getattr(services.storage, 'repository', None)
        if repository is not None:
            row = repository.get_run(str(request.request_id))
            if row and row['state'] in {'registered', 'running'}:
                code = exc.code if isinstance(exc, ExpectedServiceError) else 'generation_unavailable'
                repository.fail_run(str(request.request_id), code, 503)
        raise

