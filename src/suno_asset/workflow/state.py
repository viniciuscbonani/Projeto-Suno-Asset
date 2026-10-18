"""Estado leve do grafo, com reducers somente nos campos acumulativos."""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict

from suno_asset.domain.models import (
    Anchor, PreparedSource, ExtractionResult,
    DocumentMetadata,
    EvaluationReport,
    FactVerificationReport,
    EvidenceRef,
    ExecutionResult,
    ExecutionStatus,
    Content,
    GenerationRequest,
    PageDiagnostic,
    Revision,
    WorkflowError,
)


class WorkflowState(TypedDict, total=False):
    request: GenerationRequest
    document: DocumentMetadata
    unit_ids: tuple[str, ...]
    chunk_ids: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    anchors: tuple[Anchor, ...]
    prepared_source: PreparedSource
    extraction: ExtractionResult
    compact_factpack: object
    compact_verification: object
    compact_cache: object
    compact_cache_lock: object
    compact_cache_hit: bool
    editorial_selection: dict
    draft: Content
    evaluation: EvaluationReport
    fact_verification: FactVerificationReport
    missing_facets: tuple[str, ...]
    parse_diagnostics: tuple[PageDiagnostic, ...]
    history: Annotated[tuple[Revision, ...], add]
    errors: Annotated[tuple[WorkflowError, ...], add]
    iteration: int
    max_refinements: int
    selected_revision: int | None
    status: ExecutionStatus
    reason: str
    result: ExecutionResult

