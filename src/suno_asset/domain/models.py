"""Contratos de domínio de M1.

Os modelos não realizam I/O. Strings e IDs externos são validados na fronteira;
referências cruzadas entre artefatos são validadas pelos serviços do workflow.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class Audience(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class OutputFormat(StrEnum):
    ANALYTICAL_TEXT = "analytical_text"
    INFORMATIONAL_CAROUSEL = "informational_carousel"
    SHORT_VIDEO_SCRIPT = "short_video_script"


class SourceOrigin(StrEnum):
    UPLOAD = "upload"
    WEB = "web"
    TEXT = "text"


class ExecutionMode(StrEnum):
    SIMULATED = "simulated"
    REAL = "real"


class Architecture(StrEnum):
    CURRENT = "current"
    LITERAL = "literal-v1"


class ModelProfile(StrEnum):
    SINGLE = "single"
    QUALITY = "quality"
    HYBRID = "hybrid"
    ECONOMY = "economy"


class ExecutionStatus(StrEnum):
    RUNNING = "running"
    APPROVED = "approved"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class Importance(StrEnum):
    CRITICAL = "critical"
    SUPPORT = "support"


class AnchorKind(StrEnum):
    DECISION = "decision"
    NUMBER = "number"
    RATIONALE = "rationale"
    RISK = "risk"
    OUTLOOK = "outlook"


class CheckResult(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_VERIFIED = "not_verified"


class SourceRequest(StrictModel):
    source_id: UUID
    origin: SourceOrigin
    artifact_ref: str = Field(min_length=1)
    original_url: str | None = None
    accessed_at: datetime | None = None
    original_filename: str | None = None

    @model_validator(mode="after")
    def check_origin(self) -> Self:
        if self.origin == SourceOrigin.WEB:
            if not self.original_url or not self.original_url.startswith("https://"):
                raise ValueError("documento web exige URL HTTPS original")
            if self.accessed_at is None:
                raise ValueError("documento web exige data de acesso")
        if self.accessed_at is not None and self.accessed_at.utcoffset() is None:
            raise ValueError("data de acesso precisa conter fuso")
        return self


class GenerationRequest(StrictModel):
    request_id: UUID
    source: SourceRequest
    audience: Audience = Audience.BEGINNER
    output_format: OutputFormat = OutputFormat.ANALYTICAL_TEXT
    editorial_focus: str | None = None
    max_refinements: int = Field(default=2, ge=0, le=2)
    mode: ExecutionMode = ExecutionMode.SIMULATED
    architecture: Architecture = Architecture.CURRENT
    model_profile: ModelProfile = ModelProfile.SINGLE
    resolved_configuration: dict = Field(default_factory=dict)


class DocumentMetadata(StrictModel):
    document_id: UUID
    source_id: UUID
    origin: SourceOrigin
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_ref: str = Field(min_length=1)
    original_url: str | None = None
    accessed_at: datetime | None = None
    original_filename: str | None = None
    published_on: date | None = None
    event_on: date | None = None
    parser_version: str | None = None

    @model_validator(mode="after")
    def check_origin(self) -> Self:
        if self.origin == SourceOrigin.WEB:
            if not self.original_url or not self.original_url.startswith("https://"):
                raise ValueError("documento web exige URL HTTPS original")
            if self.accessed_at is None:
                raise ValueError("documento web exige data de acesso")
        if self.accessed_at is not None and self.accessed_at.utcoffset() is None:
            raise ValueError("data de acesso precisa conter fuso")
        return self


class Span(StrictModel):
    page: int | None = Field(default=None, ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("fim do intervalo precisa ser maior que início")
        return self


class ParsedUnit(Span):
    unit_id: str = Field(min_length=1)
    document_id: UUID
    text: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def text_matches_offsets(self) -> Self:
        if self.end - self.start != len(self.text):
            raise ValueError("intervalo da unidade não corresponde ao texto extraído")
        return self


class PageDiagnostic(StrictModel):
    page: int = Field(ge=1)
    status: str = Field(min_length=1)
    extracted_chars: int = Field(ge=0)
    image_count: int = Field(ge=0)
    note: str | None = None


class ParseResult(StrictModel):
    parser_version: str = Field(min_length=1)
    units: tuple[ParsedUnit, ...]
    diagnostics: tuple[PageDiagnostic, ...] = ()


class Chunk(Span):
    chunk_id: str = Field(min_length=1)
    document_id: UUID
    unit_id: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def text_matches_offsets(self) -> Self:
        if self.end - self.start != len(self.text):
            raise ValueError("intervalo do chunk não corresponde ao texto extraído")
        return self


class EvidenceRef(Span):
    document_id: UUID
    chunk_id: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)


class Evidence(StrictModel):
    evidence_id: str = Field(min_length=1)
    ref: EvidenceRef
    text: str = Field(min_length=1)
    retrieval_score: int = Field(default=0, ge=0)


class SourceExcerpt(StrictModel):
    excerpt_id: str = Field(min_length=1)
    unit_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    ref: EvidenceRef

    @model_validator(mode="after")
    def literal_length(self) -> Self:
        if self.ref.end - self.ref.start != len(self.text):
            raise ValueError("trecho diverge dos offsets")
        return self


class SourceBlock(StrictModel):
    block_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    excerpt_ids: tuple[str, ...] = Field(min_length=1)
    context_ids: tuple[str, ...] = ()
    required_context_ids: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


class PreparedSource(StrictModel):
    contract_version: str = "prepared-source-v1"
    segmentation_version: str = Field(min_length=1)
    target_source_tokens: int = Field(default=1000,ge=1,le=2000)
    max_source_tokens: int = Field(default=2000,ge=1,le=2000)
    document_id: UUID
    units: tuple[ParsedUnit, ...] = Field(min_length=1)
    excerpts: tuple[SourceExcerpt, ...] = Field(min_length=1)
    blocks: tuple[SourceBlock, ...] = Field(min_length=1)
    diagnostics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def complete_source(self) -> Self:
        units = {u.unit_id: u for u in self.units}
        excerpts = {e.excerpt_id: e for e in self.excerpts}
        if len(units) != len(self.units) or len(excerpts) != len(self.excerpts):
            raise ValueError("unidades/trechos duplicados")
        owners = [eid for b in self.blocks for eid in b.excerpt_ids]
        if len({b.block_id for b in self.blocks}) != len(self.blocks) or len(owners) != len(set(owners)) or set(owners) != set(excerpts):
            raise ValueError("cada trecho exige responsável único")
        for block in self.blocks:
            if set(block.required_context_ids) - set(block.context_ids):
                raise ValueError("contexto obrigatório fora do contexto do bloco")
            if set(block.context_ids) - set(excerpts) or set(block.context_ids) & set(block.excerpt_ids):
                raise ValueError("contexto inválido")
        for excerpt in self.excerpts:
            unit = units.get(excerpt.unit_id)
            ref = excerpt.ref
            if (unit is None or ref.document_id != self.document_id or unit.document_id != self.document_id
                or ref.page != unit.page or ref.parser_version != unit.parser_version
                or not unit.start <= ref.start < ref.end <= unit.end
                or unit.text[ref.start-unit.start:ref.end-unit.start] != excerpt.text):
                raise ValueError("evidência não corresponde literalmente à página")
        for unit in self.units:
            spans = sorted((e.ref.start-unit.start, e.ref.end-unit.start) for e in self.excerpts if e.unit_id == unit.unit_id)
            cursor = 0
            for start, end in spans:
                if start < cursor or unit.text[cursor:start].strip():
                    raise ValueError("fonte omitida ou sobreposta")
                cursor = end
            if unit.text[cursor:].strip():
                raise ValueError("fonte omitida")
        return self


class LiteralNumericFact(StrictModel):
    original: str = Field(min_length=1)
    excerpt_ids: tuple[str, ...] = Field(min_length=1)
    indicator: str | None = None
    unit: str | None = None
    period: str | None = None
    attribution: str | None = None
    qualifiers: tuple[str, ...] = ()


class NumericFact(StrictModel):
    original: str = Field(min_length=1)
    value: Decimal
    unit: str | None = None
    scale: str | None = None
    entity: str | None = None
    period: str | None = None
    comparison: str | None = None
    qualifiers: tuple[str, ...] = ()


class Anchor(StrictModel):
    anchor_id: str = Field(min_length=1)
    kind: AnchorKind
    importance: Importance
    statement: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    numeric_fact: NumericFact | None = None
    support_excerpts: tuple[SourceExcerpt, ...] = ()
    numeric_facts: tuple[LiteralNumericFact, ...] = ()
    owner_block_id: str | None = None
    extraction_version: str | None = None


class BlockProcessing(StrictModel):
    block_id: str = Field(min_length=1)
    classification: str = Field(pattern=r"^(material|administrative|no_relevant_facts)$")
    reason: str = Field(min_length=1)
    anchor_ids: tuple[str, ...] = ()
    batch_id: str = Field(min_length=1)


class ExtractionResult(StrictModel):
    contract_version: str = "extraction-v1"
    anchors: tuple[Anchor, ...]
    processing: tuple[BlockProcessing, ...]
    diagnostics: tuple[str, ...] = ()
    corrections: tuple[dict, ...] = ()


class AnchorVerdict(StrictModel):
    anchor_id: str = Field(min_length=1)
    result: CheckResult
    reason: str = Field(min_length=1)


class BlockVerification(StrictModel):
    block_id: str = Field(min_length=1)
    support: CheckResult
    coverage: CheckResult
    relevance: CheckResult
    reason: str = Field(min_length=1)
    anchors: tuple[AnchorVerdict, ...]
    omission_excerpt_ids: tuple[str, ...] = ()


class Claim(StrictModel):
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    anchor_ids: tuple[str, ...] = ()


class GeneratedContent(StrictModel):
    content_id: UUID
    revision: int = Field(ge=0)
    audience: Audience
    output_format: Literal[OutputFormat.ANALYTICAL_TEXT] = OutputFormat.ANALYTICAL_TEXT
    title: str = Field(min_length=1)
    synthesis: str = Field(min_length=1)
    development: str = Field(min_length=1)
    implications: str = Field(min_length=1)
    claims: tuple[Claim, ...] = Field(min_length=1)

    @property
    def references(self) -> tuple[EvidenceRef, ...]:
        return tuple(dict.fromkeys(ref for claim in self.claims for ref in claim.evidence_refs))


class ContentBlock(StrictModel):
    """Display text is bound to the exact claims evaluated for this block."""
    text: str = Field(min_length=1)
    claim_ids: tuple[str, ...] = Field(min_length=1)
    educational_notes: tuple[str, ...] = ()


class CarouselCard(ContentBlock):
    order: int = Field(ge=1)


class SpokenBlock(ContentBlock):
    pause_seconds: float = Field(default=0, ge=0)


class VisualSupport(ContentBlock):
    pass


class StructuredContent(StrictModel):
    content_id: UUID
    revision: int = Field(ge=0)
    audience: Audience
    title: str = Field(min_length=1)
    claims: tuple[Claim, ...] = Field(min_length=1)

    @property
    def references(self) -> tuple[EvidenceRef, ...]:
        return tuple(dict.fromkeys(ref for claim in self.claims for ref in claim.evidence_refs))

    @model_validator(mode="after")
    def bound_blocks(self) -> Self:
        by_id = {claim.claim_id: claim for claim in self.claims}
        if len(by_id) != len(self.claims):
            raise ValueError("IDs de afirmações duplicados")
        used = set()
        for block in self.display_blocks:
            if len(set(block.claim_ids)) != len(block.claim_ids) or set(block.claim_ids) - set(by_id):
                raise ValueError("bloco aponta para afirmações inexistentes ou duplicadas")
            expected = " ".join(by_id[cid].text for cid in block.claim_ids)
            if block.text != expected:
                raise ValueError("texto exibido diverge das afirmações avaliadas")
            used.update(block.claim_ids)
        if used != set(by_id):
            raise ValueError("afirmação ausente do conteúdo exibido")
        return self


class CarouselContent(StructuredContent):
    output_format: Literal[OutputFormat.INFORMATIONAL_CAROUSEL] = OutputFormat.INFORMATIONAL_CAROUSEL
    hook: ContentBlock
    cards: tuple[CarouselCard, ...] = Field(min_length=1)
    conclusion: ContentBlock

    @property
    def display_blocks(self):
        return (self.hook, *self.cards, self.conclusion)

    @model_validator(mode="after")
    def ordered_cards(self) -> Self:
        if tuple(card.order for card in self.cards) != tuple(range(1, len(self.cards) + 1)):
            raise ValueError("cards devem ter ordem consecutiva")
        return self


class ShortVideoContent(StructuredContent):
    output_format: Literal[OutputFormat.SHORT_VIDEO_SCRIPT] = OutputFormat.SHORT_VIDEO_SCRIPT
    hook: ContentBlock
    spoken_blocks: tuple[SpokenBlock, ...] = Field(min_length=1)
    visual_support: tuple[VisualSupport, ...] = Field(min_length=1)
    estimated_duration_seconds: float = Field(gt=0)

    @property
    def display_blocks(self):
        return (self.hook, *self.spoken_blocks, *self.visual_support)

    @property
    def computed_duration_seconds(self) -> float:
        words = len(self.hook.text.split()) + sum(len(note.split()) for note in self.hook.educational_notes)
        words += sum(len(block.text.split()) + sum(len(note.split()) for note in block.educational_notes) for block in self.spoken_blocks)
        return words * 60 / 130 + sum(block.pause_seconds for block in self.spoken_blocks)

    @model_validator(mode="after")
    def conservative_duration(self) -> Self:
        if abs(self.estimated_duration_seconds - self.computed_duration_seconds) > 0.01:
            raise ValueError("duração deve usar 130 palavras/minuto e somar pausas")
        return self


Content = Annotated[GeneratedContent | CarouselContent | ShortVideoContent, Field(discriminator="output_format")]


class EvaluationIssue(StrictModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    correctable: bool
    hard_failure: bool = False


class EvaluationCheck(StrictModel):
    name: str = Field(min_length=1)
    result: CheckResult
    applicable: bool = True
    reason: str | None = None

    @model_validator(mode="after")
    def check_applicability(self) -> Self:
        if not self.applicable and self.result != CheckResult.NOT_VERIFIED:
            raise ValueError("checagem não aplicável não pode constar como aprovada")
        if self.result == CheckResult.NOT_VERIFIED and not self.reason:
            raise ValueError("checagem inconclusiva exige motivo")
        return self


class EvaluationPolicy(StrictModel):
    version: str = Field(min_length=1)
    required_checks: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_checks(self) -> Self:
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError("checagens obrigatórias duplicadas")
        return self


class SourceMaterialityCheck(StrictModel):
    block_id: str = Field(min_length=1)
    classification: Literal['material','administrative','no_relevant_facts','not_verified']
    material_excerpt_ids: tuple[str, ...] = ()
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def consistent_materiality(self) -> Self:
        if len(set(self.material_excerpt_ids)) != len(self.material_excerpt_ids):
            raise ValueError('trechos materiais duplicados')
        if self.classification == 'material' and not self.material_excerpt_ids:
            raise ValueError('materialidade exige suporte original')
        if self.classification in {'administrative','no_relevant_facts'} and self.material_excerpt_ids:
            raise ValueError('bloco não material não pode declarar fatos materiais')
        return self


class FactVerificationReport(StrictModel):
    policy_version: str = Field(min_length=1)
    anchor_ids: tuple[str, ...]
    checks: tuple[EvaluationCheck, ...]
    issues: tuple[EvaluationIssue, ...] = ()
    approved: bool
    block_verifications: tuple[BlockVerification, ...] = ()
    processing_complete: bool | None = None
    source_materiality_complete: bool | None = None
    source_materiality_checks: tuple[SourceMaterialityCheck, ...] = ()

    @model_validator(mode="after")
    def check_approval(self) -> Self:
        if len(set(self.anchor_ids)) != len(self.anchor_ids):
            raise ValueError("âncoras duplicadas")
        if len({row.block_id for row in self.source_materiality_checks}) != len(self.source_materiality_checks):
            raise ValueError('materialidade de fonte duplicada')
        if self.approved and (self.source_materiality_complete is not None or self.source_materiality_checks) and (
            self.source_materiality_complete is not True or not self.source_materiality_checks
            or any(row.classification=='not_verified' for row in self.source_materiality_checks)):
            raise ValueError('aprovação exige controle completo de materialidade da fonte')
        if self.block_verifications:
            if len({b.block_id for b in self.block_verifications}) != len(self.block_verifications):
                raise ValueError('pareceres de bloco duplicados')
            if self.approved and (self.processing_complete is not True or any(
                b.support != CheckResult.PASSED or b.coverage != CheckResult.PASSED or b.relevance != CheckResult.PASSED
                or b.omission_excerpt_ids or any(a.result != CheckResult.PASSED for a in b.anchors)
                for b in self.block_verifications)):
                raise ValueError('aprovação exige processamento, sustentação, cobertura e relevância')
        if self.approved and (
            not self.anchor_ids
            or any(item.hard_failure for item in self.issues)
            or any(item.result != CheckResult.PASSED for item in self.checks if item.applicable)
            or not self.checks
        ):
            raise ValueError("fatos não verificáveis não podem ser aprovados")
        return self


class CriticalFactPreservation(StrictModel):
    anchor_id: str = Field(min_length=1)
    result: CheckResult
    reason: str = Field(min_length=1)
    claim_ids: tuple[str, ...] = ()


class EvaluationReport(StrictModel):
    revision: int = Field(ge=0)
    critical_facts: tuple[CriticalFactPreservation, ...] = ()
    policy: EvaluationPolicy
    checks: tuple[EvaluationCheck, ...]
    issues: tuple[EvaluationIssue, ...] = ()
    deferred_due_to_format: bool = False
    approved: bool

    @model_validator(mode="after")
    def check_approval(self) -> Self:
        if len({f.anchor_id for f in self.critical_facts}) != len(self.critical_facts):
            raise ValueError('fatos críticos duplicados')
        if self.approved and any(f.result != CheckResult.PASSED for f in self.critical_facts):
            raise ValueError('aprovação exige preservação individual dos fatos críticos')
        if self.approved and self.deferred_due_to_format:
            raise ValueError('avaliação adiada por formato não pode ser aprovada')
        by_name = {check.name: check for check in self.checks}
        if len(by_name) != len(self.checks):
            raise ValueError("checagens duplicadas")
        if self.approved and (
            any(issue.hard_failure for issue in self.issues)
            or any(
                name not in by_name
                or not by_name[name].applicable
                or by_name[name].result != CheckResult.PASSED
                for name in self.policy.required_checks
            )
        ):
            raise ValueError("aprovação exige todas as checagens obrigatórias e nenhuma falha crítica")
        return self


class WorkflowError(StrictModel):
    stage: str = Field(min_length=1)
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class Revision(StrictModel):
    number: int = Field(ge=0)
    content: Content | None = None
    evaluation: EvaluationReport | None = None
    error: WorkflowError | None = None

    @model_validator(mode="after")
    def consistent_revision(self) -> Self:
        if self.content is None and self.error is None:
            raise ValueError("tentativa sem conteúdo precisa de erro")
        if self.content is not None and self.content.revision != self.number:
            raise ValueError("revisão do conteúdo divergente")
        if self.evaluation is not None:
            if self.content is None or self.evaluation.revision != self.number:
                raise ValueError("avaliação de outra revisão")
        if self.evaluation is None and self.content is not None and self.error is None:
            raise ValueError("conteúdo sem avaliação precisa registrar erro")
        return self


class LLMCall(StrictModel):
    stage: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    latency_ms: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    at: datetime


class ExecutionResult(StrictModel):
    request_id: UUID
    architecture: Architecture = Architecture.CURRENT
    model_profile: ModelProfile = ModelProfile.SINGLE
    manifest: dict = Field(default_factory=dict)
    prepared_source: PreparedSource | None = None
    extraction: ExtractionResult | None = None
    document_id: UUID | None = None
    mode: ExecutionMode
    status: ExecutionStatus
    reason: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    selected_revision: int | None = None
    fact_verification: FactVerificationReport | None = None
    missing_facets: tuple[str, ...] = ()
    parse_diagnostics: tuple[PageDiagnostic, ...] = ()
    content: Content | None = None
    evaluation: EvaluationReport | None = None
    references: tuple[EvidenceRef, ...] = ()
    history: tuple[Revision, ...] = ()
    errors: tuple[WorkflowError, ...] = ()
    llm_calls: tuple[LLMCall, ...] = ()
    llm_attempts: int = Field(default=0, ge=0)
    llm_reserved_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        if self.status == ExecutionStatus.RUNNING:
            raise ValueError("resultado não pode estar em execução")
        if self.status == ExecutionStatus.FAILED and self.content is not None:
            raise ValueError("failed não pode entregar conteúdo válido")
        if self.status == ExecutionStatus.FAILED and (
            self.selected_revision is not None or self.evaluation is not None or self.references
        ):
            raise ValueError("failed não pode referenciar conteúdo selecionado")
        if self.status in (ExecutionStatus.APPROVED, ExecutionStatus.NEEDS_REVIEW):
            if self.content is None or self.selected_revision is None:
                raise ValueError("resultado inspecionável exige conteúdo e revisão")
            if self.content.revision != self.selected_revision:
                raise ValueError("revisão entregue divergente")
            if self.references != self.content.references:
                raise ValueError("referências entregues divergem do conteúdo")
            if not any(
                item.number == self.selected_revision and item.content == self.content
                and item.evaluation == self.evaluation
                for item in self.history
            ):
                raise ValueError("revisão entregue não consta no histórico")
        if self.fact_verification is not None and self.fact_verification.policy_version != self.policy_version:
            raise ValueError("versão da verificação dos fatos divergente")
        if self.evaluation is not None:
            if self.selected_revision is None or self.evaluation.revision != self.selected_revision:
                raise ValueError("avaliação entregue de outra revisão")
            if self.evaluation.policy.version != self.policy_version:
                raise ValueError("versão da política divergente")
        if self.status == ExecutionStatus.APPROVED and (
            self.evaluation is None or not self.evaluation.approved
            or self.fact_verification is None or not self.fact_verification.approved
        ):
            raise ValueError("approved exige avaliação aprovada")
        return self
