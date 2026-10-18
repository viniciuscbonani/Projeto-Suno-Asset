"""Verificações conservadoras do fluxo simulado; sem julgamento semântico geral."""
from __future__ import annotations
import re
from uuid import NAMESPACE_URL, uuid5
from suno_asset.domain.models import (Anchor, CheckResult, Claim, EvaluationCheck, EvaluationIssue, EvaluationPolicy, EvaluationReport, Evidence, FactVerificationReport, GeneratedContent, GenerationRequest, Importance)

_NUMBER = re.compile(r'(?<!\w)\d+(?:[.,]\d+)?\s*(?:%|p\.p\.)?')

def issue(code: str, message: str, correctable: bool = False) -> EvaluationIssue:
    return EvaluationIssue(code=code, message=message, correctable=correctable, hard_failure=True)

class DeterministicFactVerifier:
    def verify(self, anchors: tuple[Anchor, ...], evidence: tuple[Evidence, ...], policy: EvaluationPolicy) -> FactVerificationReport:
        by_ref = {item.ref: item.text for item in evidence}
        problems = []
        if not anchors or not any(a.importance == Importance.CRITICAL for a in anchors):
            problems.append(issue('critical_anchor_missing', 'nenhum fato crítico verificável'))
        if len({a.anchor_id for a in anchors}) != len(anchors):
            problems.append(issue('duplicate_anchor', 'âncoras repetidas'))
        for anchor in anchors:
            if not any(anchor.statement in by_ref.get(ref, '') for ref in anchor.evidence_refs):
                problems.append(issue('unsupported_anchor', f'âncora {anchor.anchor_id} sem suporte literal'))
            if anchor.numeric_fact and not any(anchor.numeric_fact.original in by_ref.get(ref, '') for ref in anchor.evidence_refs):
                problems.append(issue('unsupported_number', f'número da âncora {anchor.anchor_id} sem suporte'))
        passed = not problems
        return FactVerificationReport(policy_version=policy.version, anchor_ids=tuple(a.anchor_id for a in anchors), checks=(EvaluationCheck(name='fact_support', result=CheckResult.PASSED if passed else CheckResult.FAILED), EvaluationCheck(name='completeness', result=CheckResult.NOT_VERIFIED, applicable=False, reason='cobertura integral exige amostra anotada independente')),  issues=tuple(problems), approved=passed)

class DeterministicGenerator:
    def generate(self, request: GenerationRequest, anchors: tuple[Anchor, ...], evidence: tuple[Evidence, ...]) -> GeneratedContent:
        claims = tuple(Claim(claim_id=f'claim-{i}', text=a.statement, evidence_refs=a.evidence_refs) for i, a in enumerate(anchors, 1))
        return GeneratedContent(content_id=uuid5(NAMESPACE_URL, f'{request.request_id}:0'), revision=0, audience=request.audience, output_format=request.output_format, title='Trechos da fonte — simulação', synthesis=anchors[0].statement, development='\n\n'.join(a.statement for a in anchors), implications='Exemplo técnico simulado; requer revisão humana.', claims=claims)

class DeterministicEvaluator:
    def evaluate(self, content: GeneratedContent, anchors: tuple[Anchor, ...], evidence: tuple[Evidence, ...], policy: EvaluationPolicy) -> EvaluationReport:
        by_ref = {item.ref: item.text for item in evidence}
        structure = bool(content.claims) and all(c.evidence_refs for c in content.claims)
        coverage = all(any(c.text == a.statement for c in content.claims) for a in anchors if a.importance == Importance.CRITICAL)
        support = all(c.evidence_refs and any(c.text in by_ref.get(ref, '') for ref in c.evidence_refs) for c in content.claims)
        support = support and content.title == 'Trechos da fonte — simulação' and content.implications == 'Exemplo técnico simulado; requer revisão humana.'
        support = support and content.synthesis in {c.text for c in content.claims} and content.development == '\n\n'.join(c.text for c in content.claims)
        anchor_numbers = {m.group().strip() for a in anchors for m in _NUMBER.finditer(a.statement)}
        output_numbers = {m.group().strip() for c in content.claims for m in _NUMBER.finditer(c.text)}
        numbers = output_numbers <= anchor_numbers and all(all(m.group().strip() in by_ref.get(ref, '') for m in _NUMBER.finditer(c.text)) for c in content.claims for ref in c.evidence_refs)
        outcomes = {'structure': structure, 'coverage': coverage, 'support': support, 'numbers': numbers}
        problems = tuple(issue(code, message, fix) for name, code, message, fix in (('structure', 'invalid_structure', 'afirmação sem referência', True), ('coverage', 'critical_anchor_missing', 'fato crítico ausente', True), ('support', 'unsupported_claim', 'afirmação sem suporte literal', False), ('numbers', 'number_mismatch', 'número divergente', False)) if not outcomes[name])
        checks = tuple(EvaluationCheck(name=name, result=CheckResult.PASSED if passed else CheckResult.FAILED) for name, passed in outcomes.items()) + (EvaluationCheck(name='adequacy', result=CheckResult.NOT_VERIFIED, applicable=False, reason='adequação semântica ao público entra em M7'),)
        return EvaluationReport(revision=content.revision, policy=policy, checks=checks, issues=problems, approved=not problems and all(outcomes.get(name, False) for name in policy.required_checks))


class DeterministicRefiner:
    """Reconstrói texto literal quando apenas cobertura/estrutura são corrigíveis."""
    def refine(self, content, issues, anchors, evidence, revision, editorial_focus=None):
        from suno_asset.services.protocols import ExpectedServiceError
        if not issues or any(not item.correctable for item in issues):
            raise ExpectedServiceError('uncorrectable_issue', 'problema não corrigível com fatos disponíveis')
        claims = tuple(Claim(claim_id=f'claim-{i}', text=a.statement, evidence_refs=a.evidence_refs) for i, a in enumerate(anchors, 1))
        return GeneratedContent(content_id=uuid5(NAMESPACE_URL, f'{content.content_id}:{revision}'), revision=revision, audience=content.audience, output_format=content.output_format, title='Trechos da fonte — simulação', synthesis=anchors[0].statement, development='\n\n'.join(a.statement for a in anchors), implications='Exemplo técnico simulado; requer revisão humana.', claims=claims)
