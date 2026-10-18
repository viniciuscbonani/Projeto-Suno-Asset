"""Interfaces pequenas dos serviços usados pelo workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from suno_asset.domain.models import (
    Anchor,
    Chunk,
    DocumentMetadata,
    EvaluationIssue,
    EvaluationPolicy,
    EvaluationReport,
    FactVerificationReport,
    Evidence,
    EvidenceRef,
    Content,
    GenerationRequest,
    ParsedUnit,
    ParseResult,
    SourceRequest,
)


class ExpectedServiceError(Exception):
    """Falha operacional ou de entrada que vira estado estruturado."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Storage(Protocol):
    def read_raw(self, source: SourceRequest) -> bytes: ...
    def save_document(self, document: DocumentMetadata) -> None: ...
    def save_units(self, document_id: UUID, units: tuple[ParsedUnit, ...]) -> None: ...
    def save_parse_result(self, document_id: UUID, result: ParseResult) -> None: ...
    def load_units(self, document_id: UUID) -> tuple[ParsedUnit, ...]: ...
    def save_chunks(self, document_id: UUID, chunks: tuple[Chunk, ...]) -> None: ...
    def save_evidence(self, evidence: tuple[Evidence, ...]) -> None: ...
    def resolve_evidence(self, ref: EvidenceRef) -> Evidence: ...


class Parser(Protocol):
    def parse(self, raw: bytes, document: DocumentMetadata) -> ParseResult: ...


class Retriever(Protocol):
    def retrieve(
        self, document: DocumentMetadata, units: tuple[ParsedUnit, ...], focus: str | None
    ) -> tuple[tuple[Chunk, ...], tuple[Evidence, ...]]: ...


class Extractor(Protocol):
    def extract(self, evidence: tuple[Evidence, ...]) -> tuple[Anchor, ...]: ...


class FactVerifier(Protocol):
    def verify(
        self, anchors: tuple[Anchor, ...], evidence: tuple[Evidence, ...],
        policy: EvaluationPolicy,
    ) -> FactVerificationReport: ...


class Generator(Protocol):
    def generate(
        self, request: GenerationRequest, anchors: tuple[Anchor, ...], evidence: tuple[Evidence, ...]
    ) -> Content: ...


class Refiner(Protocol):
    def refine(
        self, content: Content, issues: tuple[EvaluationIssue, ...],
        anchors: tuple[Anchor, ...], evidence: tuple[Evidence, ...], revision: int,
        editorial_focus: str | None = None
    ) -> Content: ...


class Evaluator(Protocol):
    def evaluate(
        self,
        content: Content,
        anchors: tuple[Anchor, ...],
        evidence: tuple[Evidence, ...],
        policy: EvaluationPolicy,
    ) -> EvaluationReport: ...


@dataclass(frozen=True)
class Services:
    storage: Storage
    parser: Parser
    retriever: Retriever
    extractor: Extractor
    generator: Generator
    refiner: Refiner
    evaluator: Evaluator
    fact_verifier: FactVerifier
    llm_session: object | None = None
    literal_extractor: object | None = None
    literal_verifier: object | None = None
    literal_generator: object | None = None
    literal_evaluator: object | None = None
    literal_refiner: object | None = None

