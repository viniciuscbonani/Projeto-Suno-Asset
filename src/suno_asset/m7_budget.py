"""Reproduz planos de chamadas offline com fontes e saídas reais anteriores."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from suno_asset.domain.models import Anchor, GeneratedContent, GenerationRequest
from suno_asset.m7_eval import _save
from suno_asset.services.groq import GroqSession, GroqExtractor, GroqFactVerifier, GroqGenerator, GroqEvaluator, MODEL, PROMPT_VERSION
from suno_asset.services.ingestion import register_document, parse_document
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.parsing import ManualParser
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.retrieval import FullSourceRetriever
from suno_asset.workflow.graph import REAL_POLICY


class PlannedOnly(Exception): pass


class PlanningSession(GroqSession):
    plan_only = True
    def call(self, *args, **kwargs):
        raise PlannedOnly()


def aggregate_plan_budget(plans, manifest, *, errors=()):
    """Compose complete execution branches; per-stage preflight is insufficient.

    Planning never consumes session.reserved_tokens. Each recorded stage budget
    therefore needs to be added here, including its reserved retry. The future
    minimum in preflight is omitted because the actual later stages are added.
    Refinement/re-evaluation reuse the same proxy and cannot predict real drafts.
    """
    from suno_asset.m7_eval import PRICES
    stages = {}
    for plan in plans:
        stage = stages.setdefault(plan['stage'], {'tokens':0,'calls':0,'seconds':0.0,'cost_ceiling_usd':0.0,
            'base_request_tokens':0,'base_request_calls':0,'base_request_cost_usd':0.0})
        stage['tokens'] += plan['reserved_stage_tokens'] + plan['retry_reserve']
        stage['calls'] += plan['requests'] + 1
        stage['seconds'] += plan['estimated_stage_seconds']
        stage['cost_ceiling_usd'] += (plan['reserved_stage_tokens'] + plan['retry_reserve']) * max(PRICES[plan['model']]) / 1e6
        stage['base_request_tokens'] += plan['reserved_stage_tokens']
        stage['base_request_calls'] += plan['requests']
        stage['base_request_cost_usd'] += plan['reserved_stage_tokens'] * max(PRICES[plan['model']]) / 1e6
    branches = {}
    for refinements in (0,1,2):
        counts = {'extract':1,'verify':1,'generate':1,'evaluate':1+refinements,'refine':refinements}
        required = {stage for stage,n in counts.items() if n}
        missing = sorted(required - set(stages))
        values = {key:sum(stages.get(stage,{}).get(key,0)*n for stage,n in counts.items())
            for key in ('tokens','calls','seconds','cost_ceiling_usd')}
        transport_worst_tokens = sum(stages.get(stage,{}).get('base_request_tokens',0)*n*3 for stage,n in counts.items())
        transport_worst_calls = sum(stages.get(stage,{}).get('base_request_calls',0)*n*3 for stage,n in counts.items())
        transport_worst_cost = sum(stages.get(stage,{}).get('base_request_cost_usd',0)*n*3 for stage,n in counts.items())
        correction_tokens = stages.get('extract',{}).get('base_request_tokens',0)*3
        correction_calls = stages.get('extract',{}).get('base_request_calls',0)*3
        worst_tokens, worst_calls = transport_worst_tokens+correction_tokens, transport_worst_calls+correction_calls
        limits = {'tokens':manifest['max_tokens'],'calls':manifest['max_calls'],'seconds':manifest['deadline_seconds']}
        fits_limits = {key:values[key] <= cap for key,cap in limits.items()}
        stage_cost_caps=manifest.get('stage_cost_limits_usd') or {}
        stage_cost_fit={stage: stages.get(stage,{}).get('cost_ceiling_usd',0) <= cap
            for stage,cap in stage_cost_caps.items()}
        relevant_errors = [e for e in errors if e['stage'] in required]
        hard_error = any(e['code'] in {'llm_request_budget','llm_budget_exceeded','llm_deadline'} for e in relevant_errors)
        fits = False if hard_error or not all(fits_limits.values()) or not all(stage_cost_fit.values()) else None if missing else True
        branches[str(refinements)] = {'refinements':refinements,'evaluations':1+refinements,
            'stages':counts, 'reserved_tokens_with_retries':values['tokens'], 'reserved_calls_with_retries':values['calls'],
            'estimated_seconds_with_retries':round(values['seconds'],3),'cost_ceiling_usd':round(values['cost_ceiling_usd'],6),
            'caps':limits,'fits_caps':fits_limits,'complete_proxy_plan':not missing,
            'nominal_retry_policy':'one reserved retry per stage; not every retry for every request',
            'transport_worst_proxy':{'attempts_per_request':3,'reserved_tokens':transport_worst_tokens,
                'reserved_calls':transport_worst_calls,'cost_ceiling_usd':round(transport_worst_cost,6)},
            'transport_and_extraction_correction_proxy':{'reserved_tokens':worst_tokens,'reserved_calls':worst_calls,
                'correction_requests_per_extraction_batch':1,'correction_prompt_overhead':'unknown',
                'fits_token_cap':worst_tokens<=manifest['max_tokens'],'fits_call_cap':worst_calls<=manifest['max_calls'],
                'limitation':'Proxy mínimo para correção adicional; prompt corretivo e saída real são desconhecidos.'},
            'stage_cost_caps_usd':stage_cost_caps,'stage_cost_fits':stage_cost_fit,
            'missing_stage_plans':missing,'stage_errors':relevant_errors,'proxy_fits':fits,
            'status':'proxy_infeasible' if fits is False else 'proxy_unknown' if fits is None else 'proxy_fits'}
    observed_tokens = sum(v['tokens'] for v in stages.values())
    return {'version':'m7-aggregate-offline-budget-v4', 'stage_totals':stages,
        'observed_planned_stage_sum_with_retries':observed_tokens,
        'observed_planned_stages_fit_token_cap':observed_tokens <= manifest['max_tokens'],
        'branches':branches,'actual_output_feasibility':'unknown',
        'retry_completion_guaranteed':False,
        'limitation':'Orçamento de proxy literal integral, sem consumo pago. Rascunhos reais, correções de extração e revisões posteriores são desconhecidos; nenhum ramo autoriza chamadas nem aumento de limites.'}


def aggregate_round_budget(rows, max_cost_usd, max_refinements):
    branches = [row.get('aggregate_budget',{}).get('branches',{}).get(str(max_refinements)) for row in rows]
    complete = all(branch and branch['complete_proxy_plan'] for branch in branches)
    proxy_failures = sum(bool(branch and branch['proxy_fits'] is False) for branch in branches)
    proxy_unknown = sum(not branch or branch['proxy_fits'] is None for branch in branches)
    ceiling = sum(branch['cost_ceiling_usd'] for branch in branches if branch)
    return {'version':'m7-round-offline-budget-v3','runs':len(rows),'max_refinements':max_refinements,
        'complete_proxy_plan':complete,'proxy_infeasible_runs':proxy_failures,'proxy_unknown_runs':proxy_unknown,
        'known_proxy_cost_ceiling_usd':round(ceiling,6),'explicit_round_cost_cap_usd':max_cost_usd,
        'proxy_cost_fits_round_cap':False if ceiling>max_cost_usd else True if complete else None,
        'actual_output_feasibility':'unknown','unknown_usage':'Retain full attempted reservation; unknown usage never counts as zero.'}


def plan_source(source: dict, prior: Path, out: Path, model: str = MODEL) -> dict:
    raw = Path(source['local_pdf']).read_bytes()
    if sha256(raw).hexdigest() != source['sha256']:
        raise ValueError('Hash da fonte divergente')
    store = LocalStorage(out / 'artifacts')
    registered = store.put_pdf(raw, filename=Path(source['local_pdf']).name)
    document = register_document(store, registered)
    document, parsed = parse_document(store, ManualParser(), document)
    _, evidence = FullSourceRetriever().retrieve(document, parsed.units, None)
    refs = {(e.ref.page, e.ref.start, e.ref.end): e.ref for e in evidence}
    def remap(old):
        key = old.page, old.start, old.end
        if key not in refs: raise ValueError('Evidência anterior não corresponde ao parsing atual')
        return refs[key]
    name = f'{source["id"]}-{model.rsplit("/", 1)[-1]}'
    anchor_file, result_file = prior / f'{name}-anchors.json', prior / f'{name}.json'
    anchors = tuple(Anchor.model_validate(row) for row in json.loads(anchor_file.read_text())) if anchor_file.exists() else ()
    anchors = tuple(a.model_copy(update={'evidence_refs': tuple(remap(r) for r in a.evidence_refs)}) for a in anchors)
    old_result = json.loads(result_file.read_text()) if result_file.exists() else {}
    content = GeneratedContent.model_validate(old_result['content']) if old_result.get('content') else None
    if content:
        content = content.model_copy(update={'claims': tuple(c.model_copy(update={
            'evidence_refs': tuple(remap(r) for r in c.evidence_refs)}) for c in content.claims)})
    request = GenerationRequest(request_id=uuid4(), source=registered, mode='real',
        audience='beginner', output_format='analytical_text')
    session = PlanningSession('unused-offline', model=model, transport=lambda *_: None)
    actions = [('extract', lambda: GroqExtractor(session).extract(evidence))]
    if anchors:
        actions.extend([('verify', lambda: GroqFactVerifier(session).verify(anchors, evidence, REAL_POLICY)),
            ('generate', lambda: GroqGenerator(session).generate(request, anchors, evidence))])
    if content and anchors:
        actions.append(('evaluate', lambda: GroqEvaluator(session).evaluate(content, anchors, evidence, REAL_POLICY)))
    errors = []
    for stage, action in actions:
        try: action()
        except PlannedOnly: pass
        except ExpectedServiceError as exc: errors.append({'stage': stage, 'code': exc.code, 'message': str(exc)})
    return {'source_id': source['id'], 'source_sha256': source['sha256'], 'model': model,
        'prompt_version': PROMPT_VERSION, 'policy_version': REAL_POLICY.version, 'paid': False,
        'parsed_units': len(parsed.units), 'evidence_chunks': len(evidence),
        'prior_anchors_sha256': sha256(anchor_file.read_bytes()).hexdigest() if anchor_file.exists() else None,
        'prior_result_sha256': sha256(result_file.read_bytes()).hexdigest() if result_file.exists() else None,
        'stages_replayed': [stage for stage, _ in actions], 'plans': session.plans, 'errors': errors,
        'limitation': 'Planejamento com âncoras/rascunho anteriores; saída futura desconhecida. Não mede qualidade.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=Path('tests/fixtures/evaluation/corpus.json'))
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists(): parser.error('--out precisa ser uma pasta nova')
    corpus = json.loads(args.corpus.read_text())
    rows = [plan_source(source, args.prior, args.out, model='openai/gpt-oss-120b')
        for source in corpus['sources'] if source['split'] == 'development']
    _save(args.out / 'summary.json', {'paid': False, 'rows': rows})
    print(json.dumps({'paid': False, 'rows': [{key: row[key] for key in
        ('source_id', 'stages_replayed', 'errors')} for row in rows]}, ensure_ascii=False))


if __name__ == '__main__': main()
