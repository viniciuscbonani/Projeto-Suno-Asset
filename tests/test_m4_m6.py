"""Aceite determinístico de recuperação e dos dois controles separados."""
from dataclasses import replace
from uuid import UUID

from suno_asset.domain.models import (Anchor, AnchorKind, Audience, Chunk, DocumentMetadata, Evidence, EvidenceRef, ExecutionStatus, GenerationRequest, Importance, OutputFormat, ParsedUnit, SourceOrigin, SourceRequest)
from suno_asset.services.deterministic import DeterministicEvaluator, DeterministicFactVerifier, DeterministicGenerator
from suno_asset.services.fakes import MemoryStorage, fake_services
from suno_asset.services.retrieval import DeterministicExtractor, LexicalRetriever, chunk_units
from suno_asset.workflow.graph import SIMULATED_POLICY, run_workflow


def document() -> DocumentMetadata:
    return DocumentMetadata(document_id=UUID(int=3), source_id=UUID(int=2), origin=SourceOrigin.UPLOAD, sha256='a' * 64, artifact_ref='fixture', parser_version='test-v1')


def unit(page: int, text: str) -> ParsedUnit:
    return ParsedUnit(unit_id=f'page-{page}', document_id=UUID(int=3), page=page, start=0, end=len(text), text=text, parser_version='test-v1')


def test_chunk_ids_offsets_pages_and_repeatability() -> None:
    units = (unit(1, 'Primeiro parágrafo.\n\n' + 'Selic 13,75%. ' * 180), unit(2, 'Riscos de alta e de baixa.'))
    first = chunk_units(document(), units)
    assert first == chunk_units(document(), units)
    assert len({c.chunk_id for c in first}) == len(first)
    assert len(first) > 2
    for chunk in first:
        source = units[(chunk.page or 1) - 1]
        assert source.text[chunk.start:chunk.end] == chunk.text
        assert chunk.parser_version == source.parser_version
    assert {c.page for c in first} == {1, 2}


def test_lexical_facets_keep_adjacent_context_and_focus_cannot_remove_core() -> None:
    units = (unit(1, 'O Copom decidiu reduzir a Selic para 13,75%. Riscos de alta'),
             unit(2, 'e de baixa permanecem no cenário. A projeção do IPCA é 3,9% em 2027.'))
    retriever = LexicalRetriever()
    chunks, evidence = retriever.retrieve(document(), units, 'assunto inexistente')
    assert chunks and evidence
    assert {item.ref.page for item in evidence} == {1, 2}
    assert any('13,75%' in item.text for item in evidence)
    assert any('3,9%' in item.text for item in evidence)
    assert all(item.text == next(c for c in chunks if c.chunk_id == item.ref.chunk_id).text for item in evidence)
    assert 'decision' not in retriever.last_missing_facets
    retriever.retrieve(document(), (unit(1, 'Somente texto neutro.'),), None)
    assert 'decision' in retriever.last_missing_facets


def test_precheck_blocks_generator_and_keeps_own_report() -> None:
    storage = MemoryStorage()
    storage.register_raw('fixture', b'Copom decidiu manter a Selic em 10,50%.')
    request = GenerationRequest(request_id=UUID(int=5), source=SourceRequest(source_id=UUID(int=2), origin=SourceOrigin.UPLOAD, artifact_ref='fixture'), audience=Audience.BEGINNER, output_format=OutputFormat.ANALYTICAL_TEXT)
    class WrongExtractor:
        def extract(self, evidence):
            return (Anchor(anchor_id='wrong', kind=AnchorKind.DECISION, importance=Importance.CRITICAL, statement='O Copom elevou a Selic.', evidence_refs=(evidence[0].ref,)),)
    class SpyGenerator:
        calls = 0
        def generate(self, request, anchors, evidence):
            self.calls += 1
            raise AssertionError('não deve gerar')
    spy = SpyGenerator()
    services = replace(fake_services(storage), extractor=WrongExtractor(), generator=spy)
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.FAILED
    assert spy.calls == 0
    assert result.fact_verification is not None and not result.fact_verification.approved
    assert result.evaluation is None and result.content is None


def test_postcheck_rejects_new_claim_number_and_changed_qualifier() -> None:
    raw = 'O Copom decidiu reduzir a Selic para 13,75% se o cenário permitir.'
    ref = EvidenceRef(document_id=UUID(int=3), chunk_id='c', parser_version='test-v1', page=1, start=0, end=len(raw))
    evidence = (Evidence(evidence_id='e', ref=ref, text=raw),)
    anchors = (Anchor(anchor_id='a', kind=AnchorKind.DECISION, importance=Importance.CRITICAL, statement=raw, evidence_refs=(ref,)),)
    source = SourceRequest(source_id=UUID(int=2), origin=SourceOrigin.UPLOAD, artifact_ref='fixture')
    request = GenerationRequest(request_id=UUID(int=5), source=source, audience=Audience.BEGINNER, output_format=OutputFormat.ANALYTICAL_TEXT)
    assert DeterministicFactVerifier().verify(anchors, evidence, SIMULATED_POLICY).approved
    content = DeterministicGenerator().generate(request, anchors, evidence)
    evaluator = DeterministicEvaluator()
    assert evaluator.evaluate(content, anchors, evidence, SIMULATED_POLICY).approved
    for changed in ('A Selic subiu para 13,75%.', 'O Copom decidiu reduzir a Selic para 14,75% se o cenário permitir.', 'O Copom decidiu reduzir a Selic para 13,75% sem condição.', 'O Copom decidiu reduzir a Selic para 13,75 p.p. se o cenário permitir.', 'O Copom decidiu reduzir a Selic para 13,75% em 2027 se o cenário permitir.'):
        claim = content.claims[0].model_copy(update={'text': changed})
        altered = content.model_copy(update={'claims': (claim,), 'development': changed})
        assert not evaluator.evaluate(altered, anchors, evidence, SIMULATED_POLICY).approved
    altered_body = content.model_copy(update={'development': raw + ' Próximo corte garantido.'})
    assert not evaluator.evaluate(altered_body, anchors, evidence, SIMULATED_POLICY).approved


def test_inconclusive_precheck_also_blocks_generator() -> None:
    from suno_asset.domain.models import CheckResult, EvaluationCheck, FactVerificationReport
    storage = MemoryStorage()
    storage.register_raw('fixture', b'Copom decidiu manter a Selic em 10,50%.')
    request = GenerationRequest(request_id=UUID(int=5), source=SourceRequest(source_id=UUID(int=2), origin=SourceOrigin.UPLOAD, artifact_ref='fixture'), audience=Audience.BEGINNER, output_format=OutputFormat.ANALYTICAL_TEXT)
    class Inconclusive:
        def verify(self, anchors, evidence, policy):
            return FactVerificationReport(policy_version=policy.version, anchor_ids=tuple(a.anchor_id for a in anchors), checks=(EvaluationCheck(name='fact_support', result=CheckResult.NOT_VERIFIED, reason='evidência inconclusiva'),), approved=False)
    class SpyGenerator:
        calls = 0
        def generate(self, request, anchors, evidence):
            self.calls += 1
            raise AssertionError('não deve gerar')
    spy = SpyGenerator()
    result = run_workflow(request, services=replace(fake_services(storage), fact_verifier=Inconclusive(), generator=spy))
    assert result.status == ExecutionStatus.FAILED and spy.calls == 0
    assert result.fact_verification.checks[0].result == CheckResult.NOT_VERIFIED


def test_deterministic_refiner_repairs_only_known_correctable_issue() -> None:
    from suno_asset.services.deterministic import DeterministicRefiner
    from suno_asset.services.protocols import ExpectedServiceError
    import pytest
    raw = 'O Copom decidiu reduzir a Selic para 13,75% se o cenário permitir.'
    ref = EvidenceRef(document_id=UUID(int=3), chunk_id='c', parser_version='test-v1', page=1, start=0, end=len(raw))
    evidence = (Evidence(evidence_id='e', ref=ref, text=raw),)
    anchors = (Anchor(anchor_id='a', kind=AnchorKind.DECISION, importance=Importance.CRITICAL, statement=raw, evidence_refs=(ref,)),)
    request = GenerationRequest(request_id=UUID(int=5), source=SourceRequest(source_id=UUID(int=2), origin=SourceOrigin.UPLOAD, artifact_ref='fixture'), audience=Audience.BEGINNER, output_format=OutputFormat.ANALYTICAL_TEXT)
    initial = DeterministicGenerator().generate(request, anchors, evidence)
    broken = initial.model_copy(update={'claims': (initial.claims[0].model_copy(update={'text': 'O Copom decidiu reduzir'}),), 'synthesis': 'O Copom decidiu reduzir', 'development': 'O Copom decidiu reduzir'})
    evaluator = DeterministicEvaluator()
    report = evaluator.evaluate(broken, anchors, evidence, SIMULATED_POLICY)
    assert not report.approved and any(i.correctable for i in report.issues)
    repaired = DeterministicRefiner().refine(broken, report.issues, anchors, evidence, 1)
    assert repaired.revision == 1
    assert evaluator.evaluate(repaired, anchors, evidence, SIMULATED_POLICY).approved
    from suno_asset.domain.models import EvaluationIssue
    with pytest.raises(ExpectedServiceError):
        DeterministicRefiner().refine(broken, (EvaluationIssue(code='unsupported_claim', message='sem suporte', correctable=False, hard_failure=True),), anchors, evidence, 1)
