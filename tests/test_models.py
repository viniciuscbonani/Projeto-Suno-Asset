from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from suno_asset.domain.models import (
    Anchor,
    AnchorKind,
    Audience,
    CheckResult,
    Claim,
    DocumentMetadata,
    EvaluationCheck,
    EvaluationIssue,
    EvaluationPolicy,
    EvaluationReport,
    FactVerificationReport,
    EvidenceRef,
    ExecutionMode,
    ExecutionResult,
    ExecutionStatus,
    GeneratedContent,
    GenerationRequest,
    Importance,
    NumericFact,
    OutputFormat,
    Revision,
    SourceOrigin,
    SourceRequest,
    WorkflowError,
)


DOC_ID = UUID("00000000-0000-0000-0000-000000000010")
SOURCE_ID = UUID("00000000-0000-0000-0000-000000000011")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000012")
CONTENT_ID = UUID("00000000-0000-0000-0000-000000000013")
REF = EvidenceRef(document_id=DOC_ID, chunk_id="chunk-1", parser_version="fake-v1", page=1, start=0, end=10)
POLICY = EvaluationPolicy(version="simulated-v1", required_checks=("structure", "coverage"))


def valid_content() -> GeneratedContent:
    return GeneratedContent(
        content_id=CONTENT_ID,
        revision=0,
        audience=Audience.BEGINNER,
        output_format=OutputFormat.ANALYTICAL_TEXT,
        title="Copom",
        synthesis="O Copom manteve a taxa.",
        development="O Copom manteve a taxa.",
        implications="Resultado histórico.",
        claims=(Claim(claim_id="c1", text="O Copom manteve a taxa.", evidence_refs=(REF,)),),
    )


def valid_report() -> EvaluationReport:
    return EvaluationReport(
        revision=0,
        policy=POLICY,
        checks=(
            EvaluationCheck(name="structure", result=CheckResult.PASSED),
            EvaluationCheck(name="coverage", result=CheckResult.PASSED),
        ),
        approved=True,
    )


def test_domain_models_round_trip_and_numeric_precision() -> None:
    source = SourceRequest(
        source_id=SOURCE_ID,
        origin=SourceOrigin.WEB,
        artifact_ref="artifact-1",
        original_url="https://example.org/ata.pdf",
        accessed_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )
    metadata = DocumentMetadata(
        document_id=DOC_ID,
        source_id=SOURCE_ID,
        origin=source.origin,
        artifact_ref=source.artifact_ref,
        sha256="a" * 64,
        original_url=source.original_url,
        accessed_at=source.accessed_at,
    )
    number = NumericFact(original="10,50%", value=Decimal("10.50"), unit="%", period="2026")
    anchor = Anchor(
        anchor_id="a1",
        kind=AnchorKind.NUMBER,
        importance=Importance.CRITICAL,
        statement="A taxa é 10,50%.",
        evidence_refs=(REF,),
        numeric_fact=number,
    )
    for model in (source, metadata, anchor, valid_content(), valid_report()):
        assert type(model).model_validate_json(model.model_dump_json()) == model
    restored = Anchor.model_validate_json(anchor.model_dump_json())
    assert restored.numeric_fact.value == Decimal("10.50")
    assert restored.numeric_fact.original == "10,50%"


def test_origin_and_enums_are_validated() -> None:
    with pytest.raises(ValidationError):
        SourceRequest(source_id=SOURCE_ID, origin="unknown", artifact_ref="a")
    with pytest.raises(ValidationError):
        SourceRequest(source_id=SOURCE_ID, origin="web", artifact_ref="a")
    with pytest.raises(ValidationError):
        SourceRequest(
            source_id=SOURCE_ID,
            origin="web",
            artifact_ref="a",
            original_url="https://example.org/a.pdf",
            accessed_at=datetime(2026, 9, 25),
        )
    with pytest.raises(ValidationError):
        GenerationRequest(
            request_id=REQUEST_ID,
            source=SourceRequest(source_id=SOURCE_ID, origin="upload", artifact_ref="a"),
            audience="expert",
            output_format="analytical_text",
        )


def test_invalid_anchor_and_offsets_fail() -> None:
    with pytest.raises(ValidationError):
        Anchor(
            anchor_id="a1",
            kind="decision",
            importance="critical",
            statement="Decisão.",
            evidence_refs=(),
        )
    with pytest.raises(ValidationError):
        EvidenceRef(document_id=DOC_ID, chunk_id="c", parser_version="fake-v1", page=0, start=2, end=2)


def test_approval_requires_all_checks_and_no_hard_failure() -> None:
    with pytest.raises(ValidationError):
        EvaluationReport(
            revision=0, policy=POLICY, checks=(EvaluationCheck(name="structure", result="passed"),),
            approved=True,
        )
    with pytest.raises(ValidationError):
        EvaluationReport(
            revision=0,
            policy=POLICY,
            checks=(
                EvaluationCheck(name="structure", result="passed"),
                EvaluationCheck(name="coverage", result="not_verified"),
            ),
            approved=True,
        )
    with pytest.raises(ValidationError):
        EvaluationReport(
            revision=0,
            policy=POLICY,
            checks=valid_report().checks,
            issues=(EvaluationIssue(code="fatal", message="falha", correctable=False, hard_failure=True),),
            approved=True,
        )
    with pytest.raises(ValidationError):
        EvaluationCheck(name="coverage", result="passed", applicable=False)


def test_terminal_result_is_tied_to_the_evaluated_revision() -> None:
    content, report = valid_content(), valid_report()
    revision = Revision(number=0, content=content, evaluation=report)
    result = ExecutionResult(
        request_id=REQUEST_ID,
        document_id=DOC_ID,
        mode=ExecutionMode.SIMULATED,
        status=ExecutionStatus.APPROVED,
        reason="aprovado no teste",
        policy_version=POLICY.version,
        selected_revision=0,
        fact_verification=FactVerificationReport(policy_version=POLICY.version, anchor_ids=("a1",), checks=(EvaluationCheck(name="fact_support", result=CheckResult.PASSED),), approved=True),
        content=content,
        evaluation=report,
        references=content.references,
        history=(revision,),
    )
    assert ExecutionResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        ExecutionResult.model_validate(
            {**result.model_dump(mode="python"), "references": ()}
        )
    with pytest.raises(ValidationError):
        Revision(number=1, content=content, evaluation=report)
    with pytest.raises(ValidationError):
        ExecutionResult.model_validate(
            {**result.model_dump(mode="python"), "selected_revision": 1}
        )
    failed = ExecutionResult(
        request_id=REQUEST_ID,
        mode=ExecutionMode.SIMULATED,
        status=ExecutionStatus.FAILED,
        reason="arquivo ausente",
        policy_version=POLICY.version,
        errors=(WorkflowError(stage="ingest", code="missing", message="arquivo ausente"),),
    )
    assert failed.content is None and failed.selected_revision is None


def test_parser_version_keeps_evidence_references_distinct() -> None:
    from suno_asset.domain.models import Evidence
    from suno_asset.services.fakes import MemoryStorage

    older = REF
    newer = EvidenceRef(
        document_id=DOC_ID,
        chunk_id=older.chunk_id,
        parser_version="parser-v2",
        page=older.page,
        start=older.start,
        end=older.end,
    )
    storage = MemoryStorage()
    storage.save_evidence((
        Evidence(evidence_id="old", ref=older, text="Trecho anterior"),
        Evidence(evidence_id="new", ref=newer, text="Trecho reprocessado"),
    ))
    assert storage.resolve_evidence(older).text == "Trecho anterior"
    assert storage.resolve_evidence(newer).text == "Trecho reprocessado"


def test_extracted_span_must_match_its_text() -> None:
    from suno_asset.domain.models import Chunk, ParsedUnit

    with pytest.raises(ValidationError):
        ParsedUnit(
            unit_id="p1", document_id=DOC_ID, page=1,
            start=0, end=20, text="Texto", parser_version="parser-v1",
        )
    with pytest.raises(ValidationError):
        Chunk(
            chunk_id="c1", document_id=DOC_ID, unit_id="p1",
            parser_version="parser-v1", page=1, start=0, end=20, text="Texto",
        )


def test_web_document_requires_url_and_access_time() -> None:
    with pytest.raises(ValidationError):
        DocumentMetadata(
            document_id=DOC_ID,
            source_id=SOURCE_ID,
            origin=SourceOrigin.WEB,
            artifact_ref="artifact-1",
            sha256="a" * 64,
        )
