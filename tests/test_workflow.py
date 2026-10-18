from dataclasses import replace
from uuid import UUID

import pytest

from suno_asset.domain.models import (
    Architecture,
    Audience,
    CheckResult,
    ExecutionMode,
    ExecutionStatus,
    GenerationRequest,
    OutputFormat,
    SourceOrigin,
    SourceRequest,
)
from suno_asset.services.fakes import FakeParser, MemoryStorage, fake_services
from suno_asset.workflow.graph import run_workflow


RAW = b"Copom decidiu manter a taxa Selic em 10,50% ao ano."


def case(
    *,
    source_number: int = 1,
    request_number: int = 2,
    raw: bytes = RAW,
    max_refinements: int = 2,
    outcomes: tuple[str, ...] = ("pass",),
    parser_fail_code: str | None = None,
    replacement_claim: str | None = None,
):
    storage = MemoryStorage()
    storage.register_raw(f"fixture-{source_number}", raw)
    request = GenerationRequest(
        request_id=UUID(int=request_number),
        source=SourceRequest(
            source_id=UUID(int=source_number),
            origin=SourceOrigin.UPLOAD,
            artifact_ref=f"fixture-{source_number}",
        ),
        audience=Audience.BEGINNER,
        output_format=OutputFormat.ANALYTICAL_TEXT,
        max_refinements=max_refinements,
    )
    services = fake_services(
        storage,
        outcomes=outcomes,
        parser_fail_code=parser_fail_code,
        replacement_claim=replacement_claim,
    )
    return request, services


def test_first_evaluation_approves_without_refinement() -> None:
    request, services = case()
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.APPROVED
    assert result.mode == ExecutionMode.SIMULATED
    assert result.selected_revision == 0
    assert len(result.history) == 1
    assert services.refiner.calls == 0
    assert services.evaluator.calls == 1
    assert result.content == result.history[0].content
    assert result.references == result.content.references
    evidence = services.storage.resolve_evidence(result.references[0])
    assert evidence.ref.document_id == result.document_id
    assert result.content.claims[0].text in evidence.text


@pytest.mark.parametrize(
    ("limit", "status", "evaluations", "refinements"),
    [
        (0, ExecutionStatus.NEEDS_REVIEW, 1, 0),
        (1, ExecutionStatus.APPROVED, 2, 1),
        (2, ExecutionStatus.APPROVED, 2, 1),
    ],
)
def test_refinement_limit_and_early_approval(limit, status, evaluations, refinements) -> None:
    request, services = case(max_refinements=limit, outcomes=("correctable", "pass"))
    result = run_workflow(request, services=services)
    assert result.status == status
    assert services.evaluator.calls == evaluations
    assert services.refiner.calls == refinements
    assert [item.number for item in result.history] == list(range(evaluations))
    assert result.selected_revision == evaluations - 1


def test_exhausted_limit_never_approves() -> None:
    request, services = case(outcomes=("correctable",))
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.NEEDS_REVIEW
    assert [item.number for item in result.history] == [0, 1, 2]
    assert services.evaluator.calls == 3
    assert services.refiner.calls == 2
    assert result.evaluation is not None and not result.evaluation.approved


@pytest.mark.parametrize("scenario", ["uncorrectable", "inconclusive"])
def test_unfixable_or_unverified_does_not_refine(scenario: str) -> None:
    request, services = case(outcomes=(scenario, "pass"))
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.NEEDS_REVIEW
    assert result.selected_revision == 0
    assert services.evaluator.calls == 1
    assert services.refiner.calls == 0
    if scenario == "inconclusive":
        assert any(check.result == CheckResult.NOT_VERIFIED for check in result.evaluation.checks)


def test_regression_keeps_previous_reviewable_version() -> None:
    request, services = case(outcomes=("correctable", "hard_failure"))
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.NEEDS_REVIEW
    assert [item.number for item in result.history] == [0, 1]
    assert result.selected_revision == 0
    assert result.content == result.history[0].content
    assert result.evaluation == result.history[0].evaluation
    assert result.history[1].evaluation.issues[0].code == "unsupported_claim"


def test_claim_with_valid_reference_but_wrong_meaning_fails() -> None:
    request, services = case(replacement_claim="A taxa Selic caiu.", outcomes=("correctable", "pass"))
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.NEEDS_REVIEW
    assert result.selected_revision == 0
    assert any(item.code == "unsupported_claim" for item in result.history[1].evaluation.issues)


def test_expected_failures_are_structured_without_content() -> None:
    request, services = case(parser_fail_code="invalid_fixture")
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.FAILED
    assert result.content is None and result.evaluation is None
    assert result.errors[0].stage == "parse"
    assert result.errors[0].code == "invalid_fixture"
    assert services.evaluator.calls == 0


def test_evaluator_failure_after_draft_keeps_reviewable_content() -> None:
    request, services = case(outcomes=("technical_error",))
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.NEEDS_REVIEW
    assert result.selected_revision == 0
    assert result.content is not None and result.evaluation is None
    assert result.history[0].error.code == "evaluator_unavailable"
    assert result.references == result.content.references


def test_failure_after_refinement_keeps_previous_evaluation() -> None:
    request, services = case(outcomes=("correctable", "technical_error"))
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.NEEDS_REVIEW
    assert result.selected_revision == 0
    assert [item.number for item in result.history] == [0, 1]
    assert result.history[1].error.code == "evaluator_unavailable"
    assert result.evaluation == result.history[0].evaluation


def test_unexpected_programming_error_propagates() -> None:
    class BrokenParser(FakeParser):
        def parse(self, raw, document):
            raise RuntimeError("programming error")

    request, services = case()
    with pytest.raises(RuntimeError, match="programming error"):
        run_workflow(request, services=replace(services, parser=BrokenParser()))


def test_missing_artifact_returns_failed_before_document() -> None:
    request, services = case()
    services.storage.raw.clear()
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.FAILED
    assert result.document_id is None
    assert result.errors[0].code == "artifact_missing"


def test_unsupported_capability_is_rejected_before_graph() -> None:
    request, services = case()
    with pytest.raises(ValueError, match="não implementada"):
        run_workflow(request.model_copy(update={"audience": Audience.ADVANCED}), services=services)
    with pytest.raises(ValueError, match="não configurados"):
        run_workflow(request.model_copy(update={"mode": ExecutionMode.REAL}), services=services)


def test_two_requests_keep_separate_provenance() -> None:
    first_request, first_services = case(source_number=1, request_number=2)
    second_request = first_request.model_copy(
        update={
            "request_id": UUID(int=4),
            "source": first_request.source.model_copy(
                update={"source_id": UUID(int=3), "artifact_ref": "fixture-3"}
            ),
        }
    )
    first_services.storage.register_raw("fixture-3", RAW)
    first = run_workflow(first_request, services=first_services)
    second = run_workflow(second_request, services=first_services)
    assert first.document_id != second.document_id
    assert first.references[0].document_id == first.document_id
    assert second.references[0].document_id == second.document_id
    assert first_services.storage.resolve_evidence(first.references[0]).ref.document_id == first.document_id


def test_invalid_generator_contract_is_structured_failure() -> None:
    class BrokenGenerator:
        def generate(self, request, anchors, evidence):
            return {}

    request, services = case()
    result = run_workflow(request, services=replace(services, generator=BrokenGenerator()))
    assert result.status == ExecutionStatus.FAILED
    assert result.history[0].number == 0
    assert result.history[0].content is None
    assert result.errors[0].code == "invalid_service_output"


def test_failed_refinement_preserves_previous_revision() -> None:
    from suno_asset.services.protocols import ExpectedServiceError

    class BrokenRefiner:
        def refine(self, content, issues, anchors, evidence, revision):
            raise ExpectedServiceError("refiner_unavailable", "refinador indisponível")

    request, services = case(outcomes=("correctable",))
    result = run_workflow(request, services=replace(services, refiner=BrokenRefiner()))
    assert result.status == ExecutionStatus.NEEDS_REVIEW
    assert result.selected_revision == 0
    assert [item.number for item in result.history] == [0, 1]
    assert result.history[1].content is None
    assert result.errors[0].code == "refiner_unavailable"


@pytest.mark.parametrize('max_refinements,expected_status,expected_refine_calls',[
    (0,ExecutionStatus.NEEDS_REVIEW,0),(1,ExecutionStatus.APPROVED,1)])
def test_deferred_format_evaluation_can_refine_then_requires_full_reevaluation(
    max_refinements,expected_status,expected_refine_calls
):
    from suno_asset.domain.models import (CarouselCard,CriticalFactPreservation,EvaluationCheck,
        EvaluationIssue,EvaluationReport)
    from suno_asset.services.literal import SimulatedLiteralExtractor,SimulatedLiteralGenerator,SimulatedLiteralVerifier
    from suno_asset.services.source_preparation import prepare_source
    from suno_asset.workflow.graph import LITERAL_POLICY

    request,services=case(max_refinements=max_refinements)
    request=request.model_copy(update={'architecture':Architecture.LITERAL,
        'output_format':OutputFormat.INFORMATIONAL_CAROUSEL})
    class LiteralSource:
        def prepare(self,units): return prepare_source(units,None)
        def extract_prepared(self,source): return SimulatedLiteralExtractor().extract_prepared(source)
    class DeferredThenComplete:
        calls=0
        def evaluate(self,content,anchors,evidence,policy):
            self.calls+=1
            if self.calls>1:
                return EvaluationReport(revision=content.revision,policy=policy,
                    checks=tuple(EvaluationCheck(name=name,result='passed',reason='reavaliado') for name in policy.required_checks),
                    critical_facts=tuple(CriticalFactPreservation(anchor_id=a.anchor_id,result='passed',
                        reason='reavaliado',claim_ids=tuple(c.claim_id for c in content.claims if a.anchor_id in c.anchor_ids))
                        for a in anchors),approved=True)
            reason='Semântica adiada exclusivamente por formato.'
            return EvaluationReport(revision=content.revision,policy=policy,
                checks=tuple(EvaluationCheck(name=name,result='failed' if name=='structure' else 'not_verified',reason=reason)
                    for name in policy.required_checks),
                critical_facts=tuple(CriticalFactPreservation(anchor_id=a.anchor_id,result='not_verified',reason=reason) for a in anchors),
                issues=(EvaluationIssue(code='format_noncompliant',message='cinco cards obrigatórios',correctable=True),),
                deferred_due_to_format=True,approved=False)
    class CarouselRefiner:
        calls=0
        def refine(self,content,issues,anchors,evidence,revision):
            self.calls+=1
            block=content.cards[0]
            cards=tuple(CarouselCard(order=i,text=block.text,claim_ids=block.claim_ids,
                educational_notes=block.educational_notes) for i in range(1,6))
            return content.model_copy(update={'revision':revision,'cards':cards})
    evaluator=DeferredThenComplete();refiner=CarouselRefiner()
    services=replace(services,literal_extractor=LiteralSource(),literal_verifier=SimulatedLiteralVerifier(),
        literal_generator=SimulatedLiteralGenerator(),literal_evaluator=evaluator,literal_refiner=refiner)
    result=run_workflow(request,services=services,policy=LITERAL_POLICY)
    assert result.status==expected_status
    assert refiner.calls==expected_refine_calls
    if max_refinements:
        assert [revision.number for revision in result.history]==[0,1]
        assert result.evaluation.approved and result.evaluation.deferred_due_to_format is False
    else:
        assert result.selected_revision==0 and result.evaluation.deferred_due_to_format is True


def test_retrieval_rejects_evidence_that_points_outside_its_chunk() -> None:
    from suno_asset.services.fakes import FakeRetriever

    class BrokenRetriever(FakeRetriever):
        def retrieve(self, document, units, focus):
            chunks, evidence = super().retrieve(document, units, focus)
            wrong_ref = evidence[0].ref.model_copy(update={"parser_version": "different-parser"})
            return chunks, (evidence[0].model_copy(update={"ref": wrong_ref}),)

    request, services = case()
    result = run_workflow(request, services=replace(services, retriever=BrokenRetriever()))
    assert result.status == ExecutionStatus.FAILED
    assert result.errors[0].code == "invalid_retrieval"
    assert result.history == ()
