"""Operational metadata only; content-bearing audits belong to managed artifacts."""
from __future__ import annotations

import json
import logging
import time
from uuid import UUID
from datetime import datetime, timezone

LOGGER = logging.getLogger('suno_asset.operations')
LOGGER.setLevel(logging.INFO)
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(message)s'))
    LOGGER.addHandler(handler)
SAFE_ERROR_CODES = frozenset({
    'provider_config_invalid', 'gemini_auth', 'gemini_config_invalid', 'gemini_http_error',
    'gemini_key_missing', 'gemini_model_unsupported', 'gemini_model_unavailable',
    'gemini_profile_invalid', 'gemini_rate_limit', 'gemini_request_invalid', 'gemini_unavailable',
    'gemini_schema_invalid',
    'hybrid_preparation_required', 'llm_price_unknown', 'llm_execution_nominal_cap',
    'invalid_hybrid_fact', 'invalid_hybrid_verification', 'hybrid_repair_unscoped',
    'invalid_hybrid_selection', 'invalid_hybrid_review',
    'anchors_missing', 'artifact_conflict', 'artifact_integrity_error', 'artifact_missing',
    'artifact_unavailable', 'audit_missing', 'critical_anchor_missing', 'deletion_incomplete',
    'document_identity_conflict', 'evaluator_unavailable', 'evidence_missing',
    'execution_capacity_exhausted', 'execution_deadline', 'execution_identity_conflict',
    'execution_interrupted', 'free_quota_deferred', 'free_quota_request_too_large',
    'free_quota_required', 'free_quota_unverified', 'generation_unavailable', 'groq_auth',
    'groq_config_invalid', 'groq_http_error', 'groq_key_missing', 'groq_model_unsupported',
    'groq_profile_invalid', 'groq_rate_limit', 'groq_request_too_large', 'groq_unavailable',
    'idempotency_conflict', 'immutable_artifact', 'invalid_anchors', 'invalid_artifact',
    'invalid_artifact_ref', 'invalid_attempt', 'invalid_batch_plan', 'invalid_block_classification',
    'invalid_block_processing', 'invalid_block_verification', 'invalid_claim_verification',
    'invalid_critical_verification', 'invalid_evaluation', 'invalid_event', 'invalid_evidence',
    'invalid_evidence_reference', 'invalid_excerpt_reference', 'invalid_extension',
    'invalid_fact_verification', 'invalid_filename', 'invalid_generation', 'invalid_literal_anchor',
    'invalid_llm_response', 'invalid_mime', 'invalid_mode', 'invalid_numeric_reference',
    'invalid_parsing', 'invalid_pdf', 'invalid_refinement', 'invalid_resource_id',
    'invalid_retrieval', 'invalid_service_output', 'invalid_source', 'invalid_source_materiality',
    'invalid_source_plan', 'invalid_storage_path', 'invalid_text', 'known_critical_error_approved',
    'literal_extraction_failed', 'literal_service_missing', 'llm_budget_exceeded',
    'llm_daily_budget', 'llm_deadline', 'llm_empty_batch', 'llm_refusal', 'llm_request_budget',
    'llm_stage_cost_budget', 'llm_truncated', 'page_limit', 'partial_extraction', 'pdf_protected',
    'quota_job_unbound', 'refinement_input_unavailable', 'resource_busy', 'resource_deleted',
    'resource_not_found', 'service_error', 'session_already_started', 'size_limit',
    'source_unit_too_large', 'storage_busy', 'storage_unavailable', 'text_limit',
    'text_unavailable', 'token_estimate_underflow', 'tokenizer_invalid', 'uncorrectable_issue',
    'unexpected_error', 'units_missing', 'unsafe_storage_path', 'unsupported_number',
    'unsupported_numeric_field', 'unsupported_origin', 'unsupported_schema_version',
    'unsupported_structure', 'compact_fact_repair_failed', 'editorial_selection_pending',
})
NODES = {'ingest', 'parse', 'retrieve', 'extract_anchors', 'verify_facts',
         'select_editorial_facts', 'generate', 'evaluate', 'refine', 'finalize', 'provider'}
COUNTS = {'pages', 'units', 'chunks', 'evidence', 'anchors', 'duration_ms',
          'attempt', 'prompt_tokens', 'completion_tokens', 'reserved_tokens',
          'nominal_cost_usd', 'authorized_billing_usd', 'iteration'}


def emit(storage, request_id, node, status, **metrics):
    """Reject arbitrary fields instead of trying to redact unrestricted messages."""
    request_id = str(UUID(str(request_id)))
    if node not in NODES or status not in {'started', 'completed', 'failed', 'unknown', 'received'}:
        raise ValueError('invalid operational event')
    safe = {}
    for key, value in metrics.items():
        if key in COUNTS and (value is None or type(value) in (int, float) and value >= 0):
            safe[key] = value
        elif key == 'error_code' and isinstance(value, str):
            safe[key] = value if value in SAFE_ERROR_CODES else 'service_error'
        elif key == 'usage_known' and type(value) is bool:
            safe[key] = value
        elif key == 'provider' and value in {'groq', 'gemini'}:
            safe[key] = value
        elif key == 'model' and value in {'openai/gpt-oss-120b', 'openai/gpt-oss-20b',
            'gemini-3.8-flash', 'gemini-3.6-flash', 'gemini-3.5-flash-lite'}:
            safe[key] = value
    repo = getattr(storage, 'repository', None)
    if repo is not None:
        repo.event(request_id, node, status, **safe)
    try:
        LOGGER.info(json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(), 'request_id': request_id, 'node': node, 'status': status,
                               **safe}, sort_keys=True, allow_nan=False))
    except Exception:
        # The durable event above is mandatory; an optional console handler is not.
        pass


def counts(state):
    values = {}
    for field, label in [('unit_ids', 'units'), ('chunk_ids', 'chunks'),
                         ('evidence_refs', 'evidence'), ('anchors', 'anchors')]:
        if field in state:
            values[label] = len(state[field])
    diagnostics = state.get('parse_diagnostics')
    if diagnostics:
        values['pages'] = len(diagnostics)
    return values


def instrument(storage, node_name, node):
    """Each graph visit gets its own pair, including repeated refinements/finalization."""
    def wrapped(state):
        request_id = state['request'].request_id
        emit(storage, request_id, node_name, 'started', iteration=state.get('iteration', 0))
        started = time.monotonic()
        status = 'failed'
        error_code = None
        updated = state
        try:
            repo = getattr(storage, 'repository', None)
            row = repo.get_run(str(request_id)) if repo is not None else None
            if row and time.time() >= row['deadline_at']:
                from suno_asset.services.protocols import ExpectedServiceError
                raise ExpectedServiceError('execution_deadline', 'Prazo original da execução esgotado.')
            changes = node(state)
            if row and time.time() >= row['deadline_at']:
                from suno_asset.services.protocols import ExpectedServiceError
                raise ExpectedServiceError('execution_deadline', 'Prazo original da execução esgotado.')
            updated = {**state, **changes}
            status = 'failed' if changes.get('errors') else 'completed'
            if changes.get('errors'):
                error_code = changes['errors'][-1].code
            return changes
        except Exception as exc:
            from suno_asset.services.protocols import ExpectedServiceError
            error_code = exc.code if isinstance(exc, ExpectedServiceError) else 'unexpected_error'
            raise
        finally:
            emit(storage, request_id, node_name, status,
                 duration_ms=round((time.monotonic() - started) * 1000, 3), error_code=error_code, **counts(updated))
    return wrapped


def attach_provider_audit(storage, session, request_id):
    """Journal before transport; never repeat a transport after a process restart."""
    transport = session.transport
    repo = storage.repository

    def audited(payload, api_key, timeout):
        diagnostic = session.attempt_records[-1]
        number = session.calls
        attempt_id = f'{request_id}:{number}'
        repo.record_attempt(str(request_id), attempt_id, diagnostic['stage'], payload['model'])
        storage.save_provider_artifact(request_id, number, 'request', payload)
        emit(storage, request_id, 'provider', 'started', attempt=number,
             provider=getattr(session,'provider','groq'),
             model=payload['model'], reserved_tokens=diagnostic.get('total_reserved'), usage_known=False)
        started = time.monotonic()
        try:
            response = transport(payload, api_key, timeout)
            storage.save_provider_artifact(request_id, number, 'response', response)
        except BaseException:
            repo.finish_attempt(attempt_id, status='unknown')
            emit(storage, request_id, 'provider', 'unknown', attempt=number,
                 duration_ms=round((time.monotonic() - started) * 1000, 3), usage_known=False)
            raise
        from suno_asset.services.groq import _reported_usage
        usage = (session.reported_usage(response) if hasattr(session,'reported_usage') else
            _reported_usage(response.get('usage') if isinstance(response, dict) else None))
        metrics = {'usage_known': usage is not None,
                   'prompt_tokens': usage[0] if usage else None,
                   'completion_tokens': usage[1] if usage else None}
        from suno_asset.services.pricing import PRICES
        prices = PRICES.get(payload['model'])
        cost = (usage[0] * prices[0] + usage[1] * prices[1]) / 1e6 if usage and prices else None
        metrics['nominal_cost_usd'] = cost
        repo.finish_attempt(attempt_id, status='confirmed', **metrics)
        emit(storage, request_id, 'provider', 'received', attempt=number,
             provider=getattr(session,'provider','groq'),
             duration_ms=round((time.monotonic() - started) * 1000, 3),
             model=payload['model'], authorized_billing_usd=0, **metrics)
        return response

    session.operational_transport = audited
