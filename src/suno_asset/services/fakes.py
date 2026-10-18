"""Serviços previsíveis de M2: testam o grafo, não a qualidade do conteúdo real."""

from __future__ import annotations

from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from suno_asset.domain.models import (
    Anchor,
    AnchorKind,
    CheckResult,
    Chunk,
    Claim,
    DocumentMetadata,
    EvaluationCheck,
    EvaluationIssue,
    EvaluationPolicy,
    EvaluationReport,
    Evidence,
    EvidenceRef,
    GeneratedContent,
    GenerationRequest,
    Importance,
    ParsedUnit,
    ParseResult,
    SourceRequest,
)
from suno_asset.services.protocols import ExpectedServiceError, Services
from suno_asset.services.deterministic import DeterministicFactVerifier


class MemoryStorage:
    """Armazém por instância; referências permanecem válidas durante sua vida útil."""

    def __init__(self) -> None:
        self.raw: dict[str, bytes] = {}
        self.documents: dict[UUID, DocumentMetadata] = {}
        self.units: dict[UUID, tuple[ParsedUnit, ...]] = {}
        self.parse_results: dict[UUID, ParseResult] = {}
        self.chunks: dict[UUID, tuple[Chunk, ...]] = {}
        self.evidence: dict[EvidenceRef, Evidence] = {}

    def register_raw(self, ref: str, raw: bytes) -> None:
        self.raw[ref] = raw

    def read_raw(self, source: SourceRequest) -> bytes:
        try:
            return self.raw[source.artifact_ref]
        except KeyError as exc:
            raise ExpectedServiceError("artifact_missing", "artefato não encontrado") from exc

    def save_document(self, document: DocumentMetadata) -> None:
        self.documents[document.document_id] = document

    def save_units(self, document_id: UUID, units: tuple[ParsedUnit, ...]) -> None:
        self.units[document_id] = units

    def save_parse_result(self, document_id: UUID, result: ParseResult) -> None:
        self.parse_results[document_id] = result

    def load_units(self, document_id: UUID) -> tuple[ParsedUnit, ...]:
        try:
            return self.units[document_id]
        except KeyError as exc:
            raise ExpectedServiceError("units_missing", "unidades extraídas indisponíveis") from exc

    def save_chunks(self, document_id: UUID, chunks: tuple[Chunk, ...]) -> None:
        self.chunks[document_id] = chunks

    def save_evidence(self, evidence: tuple[Evidence, ...]) -> None:
        for item in evidence:
            self.evidence[item.ref] = item

    def resolve_evidence(self, ref: EvidenceRef) -> Evidence:
        try:
            return self.evidence[ref]
        except KeyError as exc:
            raise ExpectedServiceError("evidence_missing", "evidência indisponível") from exc


class FakeParser:
    def __init__(self, *, fail_code: str | None = None) -> None:
        self.fail_code = fail_code

    def parse(self, raw: bytes, document: DocumentMetadata) -> ParseResult:
        if self.fail_code:
            raise ExpectedServiceError(self.fail_code, "falha simulada no parser")
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ExpectedServiceError("text_unavailable", "fixture sem texto utilizável") from exc
        if not text:
            raise ExpectedServiceError("text_unavailable", "fixture sem texto utilizável")
        return ParseResult(parser_version="fake-v1", units=(
            ParsedUnit(
                unit_id="page-1",
                document_id=document.document_id,
                page=1,
                start=0,
                end=len(text),
                text=text,
                parser_version="fake-v1",
            ),
        ))


class FakeRetriever:
    def retrieve(
        self, document: DocumentMetadata, units: tuple[ParsedUnit, ...], focus: str | None
    ) -> tuple[tuple[Chunk, ...], tuple[Evidence, ...]]:
        if not units:
            raise ExpectedServiceError("units_missing", "não há unidades para recuperar")
        unit = units[0]
        chunk = Chunk(
            chunk_id="page-1-chunk-1",
            document_id=document.document_id,
            unit_id=unit.unit_id,
            parser_version=unit.parser_version,
            page=unit.page,
            start=unit.start,
            end=unit.end,
            text=unit.text,
        )
        ref = EvidenceRef(
            document_id=document.document_id,
            chunk_id=chunk.chunk_id,
            parser_version=chunk.parser_version,
            page=chunk.page,
            start=chunk.start,
            end=chunk.end,
        )
        return (chunk,), (Evidence(evidence_id="evidence-1", ref=ref, text=chunk.text),)


class FakeExtractor:
    def extract(self, evidence: tuple[Evidence, ...]) -> tuple[Anchor, ...]:
        if not evidence:
            raise ExpectedServiceError("evidence_missing", "nenhuma evidência recuperada")
        return (
            Anchor(
                anchor_id="anchor-1",
                kind=AnchorKind.DECISION,
                importance=Importance.CRITICAL,
                statement=evidence[0].text,
                evidence_refs=(evidence[0].ref,),
            ),
        )


class FakeGenerator:
    def generate(
        self, request: GenerationRequest, anchors: tuple[Anchor, ...], evidence: tuple[Evidence, ...]
    ) -> GeneratedContent:
        if not anchors:
            raise ExpectedServiceError("anchors_missing", "nenhuma âncora para gerar conteúdo")
        available_refs = {item.ref for item in evidence}
        if any(ref not in available_refs for anchor in anchors for ref in anchor.evidence_refs):
            raise ExpectedServiceError("evidence_missing", "âncora sem evidência disponível")
        fact = anchors[0]
        return GeneratedContent(
            content_id=uuid5(NAMESPACE_URL, f"{request.request_id}:0"),
            revision=0,
            audience=request.audience,
            output_format=request.output_format,
            title="Decisão do Copom — simulação",
            synthesis=fact.statement,
            development=fact.statement,
            implications="Resultado técnico simulado; requer revisão editorial.",
            claims=(Claim(claim_id="claim-1", text=fact.statement, evidence_refs=fact.evidence_refs),),
        )


class FakeRefiner:
    def __init__(self, *, replacement_claim: str | None = None) -> None:
        self.replacement_claim = replacement_claim
        self.calls = 0

    def refine(
        self, content: GeneratedContent, issues: tuple[EvaluationIssue, ...],
        anchors: tuple[Anchor, ...], evidence: tuple[Evidence, ...], revision: int,
        editorial_focus: str | None = None
    ) -> GeneratedContent:
        self.calls += 1
        claim = content.claims[0]
        if self.replacement_claim:
            claim = claim.model_copy(update={"text": self.replacement_claim})
        return content.model_copy(
            update={
                "content_id": uuid5(NAMESPACE_URL, f"{content.content_id}:{revision}"),
                "revision": revision,
                "claims": (claim,),
            }
        )


Scenario = Literal[
    "pass", "correctable", "uncorrectable", "inconclusive", "hard_failure", "technical_error"
]


class FakeEvaluator:
    def __init__(self, outcomes: tuple[Scenario, ...] = ("pass",)) -> None:
        if not outcomes:
            raise ValueError("é necessário ao menos um resultado simulado")
        self.outcomes = outcomes
        self.calls = 0

    def evaluate(
        self,
        content: GeneratedContent,
        anchors: tuple[Anchor, ...],
        evidence: tuple[Evidence, ...],
        policy: EvaluationPolicy,
    ) -> EvaluationReport:
        scenario = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if scenario == "technical_error":
            raise ExpectedServiceError("evaluator_unavailable", "avaliador simulado indisponível")

        known = {item.ref: item.text for item in evidence}
        supported = all(
            claim.evidence_refs
            and any(claim.text in known.get(ref, "") for ref in claim.evidence_refs)
            for claim in content.claims
        )
        covered = all(
            any(claim.text == anchor.statement for claim in content.claims)
            for anchor in anchors
            if anchor.importance == Importance.CRITICAL
        )
        issues: list[EvaluationIssue] = []
        if scenario in ("correctable", "uncorrectable"):
            covered = False
            issues.append(
                EvaluationIssue(
                    code="simulated_coverage",
                    message="cobertura simulada insuficiente",
                    correctable=scenario == "correctable",
                    hard_failure=True,
                )
            )
        if scenario == "hard_failure":
            supported = False
        if not supported:
            issues.append(
                EvaluationIssue(
                    code="unsupported_claim",
                    message="afirmação sem suporte verificável no cenário simulado",
                    correctable=False,
                    hard_failure=True,
                )
            )
        if not covered and not any(item.code == "simulated_coverage" for item in issues):
            issues.append(
                EvaluationIssue(
                    code="critical_anchor_missing",
                    message="âncora crítica ausente",
                    correctable=True,
                    hard_failure=True,
                )
            )
        checks = (
            EvaluationCheck(name="structure", result=CheckResult.PASSED),
            EvaluationCheck(
                name="coverage", result=CheckResult.PASSED if covered else CheckResult.FAILED
            ),
            EvaluationCheck(
                name="support",
                result=(
                    CheckResult.NOT_VERIFIED
                    if scenario == "inconclusive"
                    else CheckResult.PASSED if supported else CheckResult.FAILED
                ),
                reason="sustentação simulada inconclusiva" if scenario == "inconclusive" else None,
            ),
        )
        approved = not issues and all(
            check.result == CheckResult.PASSED
            for check in checks
            if check.name in policy.required_checks
        )
        return EvaluationReport(
            revision=content.revision,
            policy=policy,
            checks=checks,
            issues=tuple(issues),
            approved=approved,
        )


def fake_services(
    storage: MemoryStorage,
    *,
    outcomes: tuple[Scenario, ...] = ("pass",),
    parser_fail_code: str | None = None,
    replacement_claim: str | None = None,
) -> Services:
    return Services(
        storage=storage,
        parser=FakeParser(fail_code=parser_fail_code),
        retriever=FakeRetriever(),
        extractor=FakeExtractor(),
        generator=FakeGenerator(),
        refiner=FakeRefiner(replacement_claim=replacement_claim),
        evaluator=FakeEvaluator(outcomes),
        fact_verifier=DeterministicFactVerifier(),
    )
