"""Comparação versionada de arquiteturas/perfis; reserva permanece fechada."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import time
from uuid import uuid4
from urllib.error import HTTPError

from suno_asset.api.app import _real_services
from suno_asset.domain.models import EvaluationIssue, GenerationRequest
from suno_asset.m7_eval import PRICES, PRICE_SOURCE, _save
from suno_asset.services.groq import MAX_TOKENS, make_session
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.workflow.graph import REAL_POLICY, LITERAL_POLICY, run_workflow

CONFIGURATIONS = {
    'current-quality': {'architecture':'current','profile':'quality'},
    'literal-quality': {'architecture':'literal-v1','profile':'quality'},
    'literal-hybrid': {'architecture':'literal-v1','profile':'hybrid'},
}


def configuration(name):
    spec=CONFIGURATIONS[name]
    return {'id':name,**spec,'models':{stage:'openai/gpt-oss-20b' if spec['profile']=='hybrid' and stage=='extract'
        else 'openai/gpt-oss-120b' for stage in ('extract','verify','generate','evaluate','refine')}}


def make_execution_protocol(purpose, max_refinements, stage_cost_limits):
    if purpose not in {'main','development-matrix','reserve-matrix'}:
        raise ValueError('Finalidade de execução inválida')
    if type(max_refinements) is not int or max_refinements not in (0,1,2):
        raise ValueError('Limite de refinamento inválido')
    return {'version':'m7-execution-protocol-v1','purpose':purpose,
        'max_refinements':max_refinements,'stage_cost_limits':dict(sorted(stage_cost_limits.items()))}


def execution_identity(source, name, audience, output_format, repetition, fingerprint_sha256, protocol):
    # Settings are immutable registration data, never a way to create another
    # job after an uncertain call. Only the explicitly different trial purpose
    # distinguishes the additional matrix from the main comparison.
    logical={'version':'m7-full-ata-job-v2','purpose':protocol['purpose'],
        'fingerprint':fingerprint_sha256,'source_id':source['id'],'configuration':name,
        'audience':audience,'output_format':output_format,'repetition':repetition}
    identity={**logical,'source_sha256':source['sha256'],'execution_protocol':protocol}
    return identity, sha256(json.dumps(logical,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def register_execution(ledger, identity, job_id):
    ledger.register_job(job_id,identity['fingerprint'],identity['source_id'],
        json.dumps(identity,ensure_ascii=False,sort_keys=True))


def _write_summary(path, value):
    """Update only batch progress; provider checkpoints/results stay immutable."""
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def cost_records(session):
    per_stage={}
    for call in session.records:
        ip,op=PRICES[call['model']]
        row=per_stage.setdefault(call['stage'],{'calls':0,'prompt_tokens':0,'completion_tokens':0,'estimated_usd':0.0,'models':[]})
        row['calls']+=1;row['prompt_tokens']+=call['prompt_tokens'];row['completion_tokens']+=call['completion_tokens']
        row['estimated_usd']+=(call['prompt_tokens']*ip+call['completion_tokens']*op)/1e6
        if call['model'] not in row['models']:row['models'].append(call['model'])
    return per_stage


def audited_transport(session, out):
    """Persist each paid attempt before/after transport, without credentials.

    A cancelled process may never reach workflow finalization. These immutable
    checkpoints preserve the requests and reported usage for that diagnosis.
    Install after session construction to retain its original shared rate gate.
    """
    transport = session.transport

    def invoke(payload, api_key, timeout):
        attempt = session.calls
        prefix = out / 'call-audit' / f'attempt-{attempt:03}'
        _save(prefix.with_name(prefix.name + '-started.json'), {
            'at': datetime.now(timezone.utc).isoformat(),
            'manifest': session.manifest(),
            'attempt': dict(session.attempt_records[-1]),
            'request': payload,
        })
        stage = session.attempt_records[-1]['stage']
        print(json.dumps({'stage': stage, 'attempt': attempt, 'transport': 'started'}), flush=True)
        try:
            raw = transport(payload, api_key, timeout)
        except BaseException as exc:
            diagnostic = {
                'at': datetime.now(timezone.utc).isoformat(),
                'exception_type': type(exc).__name__,
                'http_status': getattr(exc, 'code', None),
                'usage_known': False,
            }
            if isinstance(exc, HTTPError):
                body = exc.read()
                diagnostic['provider_response'] = body.decode('utf-8', errors='replace').replace(api_key, '[REDACTED]')
                _save(prefix.with_name(prefix.name + '-interrupted.json'), diagnostic)
                # Preserve the readable response for the session's error policy.
                raise HTTPError(exc.url, exc.code, exc.msg, exc.headers, BytesIO(body)) from exc
            _save(prefix.with_name(prefix.name + '-interrupted.json'), diagnostic)
            raise
        _save(prefix.with_name(prefix.name + '-received.json'), {
            'at': datetime.now(timezone.utc).isoformat(), 'response': raw,
        })
        print(json.dumps({'stage': stage, 'attempt': attempt, 'transport': 'received',
            'usage': raw.get('usage') if isinstance(raw, dict) else None}), flush=True)
        return raw

    return invoke


def run_case(source,name,out,max_refinements=2,*,session=None,audience='beginner',output_format='analytical_text',stage_cost_limits=None,free_only=False,quota_job_id=None,fingerprint_sha256=None,repetition=1,execution_protocol=None):
    raw=Path(source['local_pdf']).read_bytes()
    if sha256(raw).hexdigest()!=source['sha256']:raise ValueError('hash da ata divergente')
    spec=configuration(name)
    session=session or make_session(free_only=free_only)
    if session.free_only:session.require_free_quota_evidence()
    session.stage_cost_limits=dict(stage_cost_limits or {})
    protocol=execution_protocol or make_execution_protocol('main',max_refinements,session.stage_cost_limits)
    if (protocol.get('max_refinements')!=max_refinements
        or protocol.get('stage_cost_limits')!=session.stage_cost_limits):
        raise ValueError('Protocolo diverge dos limites da execução')
    quota_ledger=session.quota_ledger if session.free_only else None
    quota_root_job_id=None
    from suno_asset.m7_acceptance import technical_attempts_complete
    if quota_ledger:
        if not quota_job_id or not fingerprint_sha256:
            raise ExpectedServiceError('quota_job_unbound','execução gratuita sem job/fingerprint persistentes')
        identity,expected_id=execution_identity(source,name,audience,output_format,repetition,fingerprint_sha256,protocol)
        register_execution(quota_ledger,identity,expected_id)
        active=quota_ledger.resolve_job(expected_id)
        if quota_job_id not in {expected_id,active['job_id']}:raise ValueError('Identidade da tarefa diverge do protocolo')
        quota_root_job_id=expected_id
        quota_job_id=active['job_id']
        session.quota_job_id=quota_job_id
        if quota_job_id!=quota_root_job_id:
            out=out/'recoveries'/quota_job_id
        quota_ledger.start_job(quota_job_id)
    store=LocalStorage(out/'artifacts')
    registered=store.put_pdf(raw,filename=Path(source['local_pdf']).name)
    request=GenerationRequest(request_id=uuid4(),source=registered,mode='real',audience=audience,
        output_format=output_format,max_refinements=max_refinements,architecture=spec['architecture'],model_profile=spec['profile'],
        resolved_configuration={'execution_protocol':protocol})
    services=_real_services(store,session)
    anchors=[]
    class RecordExtractor:
        def extract(self,evidence):
            result=services.extractor.extract(evidence);anchors.extend(result);return result
    policy=LITERAL_POLICY if spec['architecture']=='literal-v1' else REAL_POLICY
    started=time.monotonic()
    transport = session.transport
    audit_root = out / 'runs' / str(request.request_id)
    session.transport = audited_transport(session, audit_root)
    try:
        result=run_workflow(request,services=replace(services,extractor=RecordExtractor()),policy=policy)
    finally:
        session.transport = transport
    deferred_before_first_call=(quota_ledger is not None and session.calls==0
        and any(a.get('deferred_code')=='free_quota_deferred' for a in session.attempt_records)
        and any(error.code=='free_quota_deferred' for error in result.errors))
    if deferred_before_first_call:
        quota_ledger.defer_job(quota_job_id)
        return {'configuration':spec,'configuration_id':name,'source_id':source['id'],
            'source_sha256':source['sha256'],'split':source['split'],'audience':audience,'repetition':repetition,
            'output_format':output_format,'status':'quota_deferred','errors':[e.code for e in result.errors],
            'attempts':0,'quota_job_id':quota_job_id,'quota_fingerprint_sha256':fingerprint_sha256,
            'quota_status':'deferred','next_window':quota_ledger.next_available_window(quota_job_id)}
    name_stem=f'{source["id"]}-{name}'
    path=out/f'{name_stem}.json'
    _save(path,result.model_dump(mode='json'))
    if result.extraction:anchors=list(result.extraction.anchors)
    _save(out/f'{name_stem}-anchors.json',[a.model_dump(mode='json') for a in anchors])
    usage=cost_records(session)
    _save(out/f'{name_stem}-budget.json',{'manifest':result.manifest,'per_stage':usage,
        'plans':session.plans,'attempts':session.attempt_records,'corrections':session.extraction_corrections,
        'events':session.literal_artifacts})
    budget_path=out/f'{name_stem}-budget.json'
    critical=[a for a in anchors if a.importance.value=='critical']
    preserved=[f for f in result.evaluation.critical_facts if f.result.value=='passed'] if result.evaluation else []
    row={'configuration':spec,'configuration_id':name,'source_id':source['id'],'source_sha256':source['sha256'],
        'execution_protocol':protocol,
        'split':source['split'],'repetition':repetition,'model':spec['models']['generate'],'architecture':spec['architecture'],'profile':spec['profile'],
        'audience':audience,'output_format':output_format,
        'status':result.status.value,'selected_revision':result.selected_revision,'errors':[e.code for e in result.errors],
        'technical_complete':bool(result.content and result.evaluation and not result.errors),
        'processing_complete':result.fact_verification.processing_complete if result.fact_verification else None,
        'critical_total':len(critical),'automatic_critical_preserved':len(preserved),
        'automatic_metrics': {name:next((c.result.value for c in result.evaluation.checks if c.name==name),None)
            if result.evaluation else None for name in ('coverage','support','numbers','citations')},
        'independent_metrics':{'critical_coverage':None,'unsupported_claims':None,'numeric_errors':None,
            'attribution_errors':None,'false_approvals':None,'false_rejections':None},
        'attempts':session.calls,'prompt_tokens':session.prompt_tokens,'completion_tokens':session.completion_tokens,
        'per_stage':usage,'estimated_usd':round(sum(v['estimated_usd'] for v in usage.values()),6),
        'usage_complete':len(session.attempt_records)==session.calls and all(a['usage_known'] for a in session.attempt_records),
        'audit_dir':str((audit_root/'call-audit').resolve()),
        'audit_hashes':{str(p.resolve()):sha256(p.read_bytes()).hexdigest() for p in (audit_root/'call-audit').rglob('*.json')},
        'wall_seconds':round(time.monotonic()-started,3),'manifest':result.manifest,'result_file':str(path.resolve()),
        'result_sha256':sha256(path.read_bytes()).hexdigest(),'budget_file':str(budget_path.resolve()),
        'budget_sha256':sha256(budget_path.read_bytes()).hexdigest(),'human_review_status':'pending'}
    row['technical_complete'] = bool(row['technical_complete'] and technical_attempts_complete(row, session.attempt_records))
    if quota_ledger:
        row.update(quota_job_id=quota_job_id,quota_fingerprint_sha256=fingerprint_sha256,
            quota_status='complete' if row['technical_complete'] else 'incomplete')
        if quota_job_id!=quota_root_job_id:
            row.update(quota_root_job_id=quota_root_job_id,
                quota_recovery_lineage=quota_ledger.recovery_lineage(quota_job_id))
            from suno_asset.m7_recovery import recovery_cost
            row['quota_recovery_nominal_usd']=recovery_cost(row)
        job_record=out/f'{name_stem}-quota-job.json'
        _save(job_record,row)
        job_hash=sha256(job_record.read_bytes()).hexdigest()
        if row['quota_status']=='complete':quota_ledger.complete_job(quota_job_id,job_record,job_hash)
        else:quota_ledger.incomplete_job(quota_job_id,job_record,job_hash)
    return row


def plan_case(source,name,out,*,audience='beginner',output_format='analytical_text',max_refinements=2,stage_cost_limits=None):
    from suno_asset.services.ingestion import register_document,parse_document
    from suno_asset.services.parsing import ManualParser
    from suno_asset.services.source_preparation import prepare_source,source_artifacts
    from suno_asset.services.literal import (LiteralExtractor,SimulatedLiteralExtractor,LiteralFactVerifier,LiteralGenerator,LiteralEvaluator,
        SimulatedLiteralGenerator)
    from suno_asset.m7_budget import PlanningSession,PlannedOnly
    raw=Path(source['local_pdf']).read_bytes()
    if sha256(raw).hexdigest()!=source['sha256']:raise ValueError('hash divergente')
    store=LocalStorage(out/'artifacts');registered=store.put_pdf(raw,filename=Path(source['local_pdf']).name)
    document=register_document(store,registered);_,parsed=parse_document(store,ManualParser(),document)
    from suno_asset.services.provider_limits import limits_from_environment
    limits=limits_from_environment()
    session=PlanningSession('unused-offline',transport=lambda *_:None,tpm_limit=limits.tokens_per_minute,
        rpm_limit=limits.requests_per_minute,rpd_limit=limits.requests_per_day,tpd_limit=limits.tokens_per_day)
    session.stage_cost_limits=dict(stage_cost_limits or {})
    spec=configuration(name);session.configure(spec['architecture'],spec['profile'])
    request=GenerationRequest(request_id=uuid4(),source=registered,audience=audience,output_format=output_format,
        architecture=spec['architecture'],model_profile=spec['profile'],max_refinements=max_refinements)
    errors=[];actions=[]
    if spec['architecture']=='literal-v1':
        prepared=LiteralExtractor(session).prepare(parsed.units)
        _,evidence=source_artifacts(prepared)
        extraction=SimulatedLiteralExtractor().extract_prepared(prepared)
        content=SimulatedLiteralGenerator().generate(request,extraction.anchors,evidence)
        _save(out/f'{source["id"]}-{name}-prepared.json',prepared.model_dump(mode='json'))
        actions=[('extract',lambda:LiteralExtractor(session).extract_prepared(prepared)),
            ('verify',lambda:LiteralFactVerifier(session).verify_prepared(prepared,extraction,LITERAL_POLICY)),
            ('generate',lambda:LiteralGenerator(session).generate(request,extraction.anchors,evidence)),
            ('evaluate',lambda:LiteralEvaluator(session).evaluate(content,extraction.anchors,evidence,LITERAL_POLICY))]
        from suno_asset.services.groq import GroqRefiner
        actions.append(('refine',lambda:GroqRefiner(LiteralGenerator(session)).refine(content,
            (EvaluationIssue(code='offline_planning',message='Planejamento de revisão, sem julgamento',correctable=True),),
            extraction.anchors,evidence,1)))
    else:
        from suno_asset.services.groq import (GroqExtractor,GroqFactVerifier,GroqGenerator,
            GroqEvaluator,GroqRefiner)
        # Use the full literal source as an input-size proxy for downstream
        # historical stages. This never substitutes for the historical model
        # output or a semantic verdict.
        prepared=LiteralExtractor(session).prepare(parsed.units)
        _,literal_evidence=source_artifacts(prepared)
        extraction=SimulatedLiteralExtractor().extract_prepared(prepared)
        evidence=literal_evidence
        content=SimulatedLiteralGenerator().generate(request,extraction.anchors,evidence)
        generator=GroqGenerator(session)
        actions=[('extract',lambda:GroqExtractor(session).extract(evidence)),
            ('verify',lambda:GroqFactVerifier(session).verify(extraction.anchors,evidence,REAL_POLICY)),
            ('generate',lambda:generator.generate(request,extraction.anchors,evidence)),
            ('evaluate',lambda:GroqEvaluator(session).evaluate(content,extraction.anchors,evidence,REAL_POLICY)),
            ('refine',lambda:GroqRefiner(generator).refine(content,
                (EvaluationIssue(code='offline_planning',message='Planejamento de revisão, sem julgamento',correctable=True),),
                extraction.anchors,evidence,1))]
    for stage,action in actions:
        try:action()
        except PlannedOnly:pass
        except Exception as exc:errors.append({'stage':stage,'code':getattr(exc,'code',type(exc).__name__),'message':str(exc)})
    from suno_asset.services.content_formats import format_errors
    from suno_asset.m7_budget import aggregate_plan_budget
    aggregate=aggregate_plan_budget(session.plans,session.manifest(),errors=errors)
    aggregate['requested_max_refinements']=max_refinements
    aggregate['requested_branch_status']=aggregate['branches'][str(max_refinements)]['status']
    if aggregate['branches'][str(max_refinements)]['proxy_fits'] is False:
        errors.append({'stage':'aggregate','code':'offline_proxy_budget_exceeded',
            'message':'Ramo solicitado, incluindo reavaliação após cada refinamento, excede teto do proxy; viabilidade da saída real permanece desconhecida.'})
    return {'configuration':spec,'configuration_id':name,'source_id':source['id'],'source_sha256':source['sha256'],'paid':False,
        'audience':audience,'output_format':output_format,
        **({'simulated_format_feasibility':{'applicable_to':'literal_copy_proxy_only',
            'issues':[issue.model_dump(mode='json') for issue in format_errors(content)]}} if spec['architecture']=='literal-v1' else {}),
        'manifest':session.manifest(),'plans':session.plans,'aggregate_budget':aggregate,'errors':errors,
        'attempted_stages':[stage for stage,_ in actions],
        'planned_stages':list(dict.fromkeys(plan['stage'] for plan in session.plans)),
        'unplannable_stages':sorted({error['stage'] for error in errors if error.get('stage') in {stage for stage,_ in actions}}),
        'limitation':'Etapas posteriores usam âncoras e conteúdo simulados com retenção integral apenas para estimar tamanho. Os ramos 0/1/2 repetem os planos de avaliação/refinamento como proxies; conteúdo real, reavaliações futuras, qualidade e saídas novas permanecem desconhecidos. Etapa rejeitada pelo preflight fica unknown e bloqueia a rodada, nunca equivale a zero.'}


def report_markdown(report):
    lines=['# Comparação de arquiteturas M7','','Fluxo atual permanece padrão até promoção válida. Revisão independente pendente; reserva fechada.','',
        '| Ata | Configuração | Status | Conclusão técnica | Chamadas/teto | Tokens entrada/saída | USD/teto | Segundos |',
        '|---|---|---|---|---|---|---|---|']
    for r in report['rows']:
        if 'status' in r:
            lines.append(f'| {r["source_id"]} | {r["configuration_id"]} | {r["status"]} | {r["technical_complete"]} | {r["attempts"]} | {r["prompt_tokens"]}/{r["completion_tokens"]} | {r["estimated_usd"]} | {r["wall_seconds"]} |')
        else:
            budget=r.get('aggregate_budget',{});branch=budget.get('branches',{}).get(str(budget.get('requested_max_refinements',0)),{})
            lines.append(f'| {r["source_id"]} | {r["configuration_id"]} | plano {branch.get("status","unknown")} | não se aplica | {branch.get("reserved_calls_with_retries","unknown")} | desconhecido/desconhecido | {branch.get("cost_ceiling_usd","unknown")} | {branch.get("estimated_seconds_with_retries","unknown")} |')
    lines+=['',f'Estado da rodada: {report["status"]}.','',
        'Cobertura crítica, erros numéricos/de atribuição, afirmações sem suporte e falsas aprovações/reprovações exigem revisão independente; campos pendentes não são zeros.',
        '',f'Tetos por etapa: `{json.dumps(report.get("stage_cost_caps_usd",{}),ensure_ascii=False)}`. Preços estimados por modelo: [Groq]({PRICE_SOURCE}). Tentativas sem uso reportado mantêm custo desconhecido/conservador.']
    if any(row.get('quota_recovery_lineage') for row in report['rows']):
        from suno_asset.m7_recovery import recovery_cost
        lines += ['', f'Custo nominal adicional das predecessoras encerradas: USD {sum(recovery_cost(row) for row in report["rows"]):.8f}. As falhas permanecem na linhagem e não contam como execuções aprovadas.']
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,default=Path('tests/fixtures/evaluation/corpus.json'))
    parser.add_argument('--configurations',nargs='+',choices=CONFIGURATIONS,default=list(CONFIGURATIONS))
    parser.add_argument('--source-id',nargs='+')
    parser.add_argument('--repeats',type=int,choices=(1,2,3),default=3)
    parser.add_argument('--max-runs',type=int,default=18)
    parser.add_argument('--max-cost-usd',type=float,required=True)
    parser.add_argument('--stage-cost-cap',action='append',default=[],metavar='STAGE=USD',
        help='Teto nominal conservador por etapa; obrigatório em execução real')
    parser.add_argument('--max-refinements',type=int,choices=(0,1,2),default=2)
    parser.add_argument('--free-only',action='store_true',help='Impõe cotas gratuitas documentadas e ledger SQLite compartilhado; não tenta mudar plano ou contratar')
    parser.add_argument('--plan',action='store_true')
    parser.add_argument('--matrix',action='store_true',help='Nove combinações em cada ata, somente candidato literal')
    parser.add_argument('--allow-heldout',action='store_true')
    parser.add_argument('--frozen',type=Path)
    parser.add_argument('--prior-summary',nargs='+',type=Path,help='Rodadas idênticas já concluídas; completar somente execuções ausentes')
    parser.add_argument('--resume-reserve',type=Path,help='Retomar combinações ainda não iniciadas no mesmo lote congelado da reserva')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    stage_cost_limits={}
    for item in args.stage_cost_cap:
        try: stage,value=item.split('=',1); value=float(value)
        except (ValueError,TypeError): parser.error('teto por etapa deve usar STAGE=USD')
        if stage not in {'extract','verify','generate','evaluate','refine'} or value<=0 or stage in stage_cost_limits:
            parser.error('etapa/teto por etapa inválido ou duplicado')
        stage_cost_limits[stage]=value
    if not args.plan and set(stage_cost_limits)!={'extract','verify','generate','evaluate','refine'}:
        parser.error('execução paga exige tetos explícitos para extract, verify, generate, evaluate e refine')
    if not args.plan and not args.free_only:
        parser.error('execução M7 real exige --free-only; chamadas comerciais não estão habilitadas neste fluxo')
    if not args.plan and args.free_only:
        from suno_asset.services.groq import SUPPORTED_MODELS
        from suno_asset.services.quota_ledger import QuotaWindow, validate_free_quota_evidence
        evidence_path=os.getenv('GROQ_FREE_QUOTA_EVIDENCE','')
        if not evidence_path:parser.error('defina GROQ_FREE_QUOTA_EVIDENCE com prova recente do plano gratuito e limites da conta')
        try:
            credential_sha256=sha256(make_session(free_only=True).api_key.encode()).hexdigest()
            free_evidence=validate_free_quota_evidence(Path(evidence_path),SUPPORTED_MODELS,QuotaWindow(),
                credential_sha256=credential_sha256)
        except ExpectedServiceError as exc:parser.error(str(exc))
    if args.resume_reserve:
        if not args.allow_heldout or not args.out.is_dir() or args.resume_reserve.resolve()!=(args.out/'summary.json').resolve():
            parser.error('--resume-reserve exige o summary.json do mesmo diretório existente e --allow-heldout')
    elif args.out.exists():parser.error('preserve relatórios: --out exige pasta nova')
    corpus=json.loads(args.corpus.read_text())
    if args.allow_heldout:
        if not args.frozen or len(args.configurations)!=1 or args.repeats!=1 or args.plan or args.prior_summary or not args.matrix or args.source_id:
            parser.error('reserva exige um único lote das nove combinações congeladas, sem seleção de fontes/reuso/plano')
        if args.resume_reserve and not args.free_only:
            parser.error('retomada da reserva exige --free-only')
        from suno_asset.m7_literal_acceptance import validate_frozen_configuration
        try:validate_frozen_configuration(args.frozen,args.corpus,args.configurations[0])
        except (ValueError,KeyError,TypeError,OSError) as exc:parser.error(str(exc))
    sources=[s for s in corpus['sources'] if s['split']==('heldout' if args.allow_heldout else 'development')]
    if args.source_id:
        if set(args.source_id)-{s['id'] for s in sources}:parser.error('fonte desconhecida/reservada')
        sources=[s for s in sources if s['id'] in args.source_id]
    if args.matrix and (len(args.configurations)!=1 or args.configurations[0]=='current-quality' or args.repeats!=1 or args.prior_summary):
        parser.error('matriz exige um candidato literal e uma repetição nova')
    from suno_asset.m7_acceptance import fingerprint, provider_calls_are_real
    current_fingerprint=fingerprint(args.corpus)
    fingerprint_sha256=sha256(json.dumps(current_fingerprint,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    protocol=make_execution_protocol('reserve-matrix' if args.allow_heldout else 'development-matrix' if args.matrix else 'main',
        args.max_refinements,stage_cost_limits)
    quota_ledger=None
    if args.free_only and not args.plan:
        from suno_asset.services.quota_ledger import FreeQuotaLedger,QuotaWindow
        limits=QuotaWindow(**free_evidence['limits'])
        quota_ledger=FreeQuotaLedger(Path(os.getenv('GROQ_FREE_QUOTA_DB','data/m7-quota/free-quota.sqlite3')),limits)
    prior_rows=[]
    prior_failed_rows=[]
    resume_report=None
    prior_paths=list(args.prior_summary or [])
    if args.resume_reserve:prior_paths.append(args.resume_reserve)
    for path in prior_paths:
        previous=json.loads(path.read_text())
        if args.resume_reserve:resume_report=previous
        if previous.get('fingerprint')!=fingerprint(args.corpus) or previous.get('max_refinements')!=args.max_refinements or not provider_calls_are_real(previous) or args.plan:
            parser.error('rodada anterior incompatível: arquitetura/código/refinamentos/plano')
        if previous.get('execution_protocol')!=protocol:
            parser.error('rodada anterior incompatível: finalidade/refinamentos/tetos de execução')
        if args.allow_heldout and (not args.resume_reserve or previous.get('heldout_included') is not True
            or previous.get('frozen_sha256')!=sha256(args.frozen.read_bytes()).hexdigest()
            or path.resolve()!=(args.out/'summary.json').resolve()):
            parser.error('retomada exige o mesmo lote reserva, resumo, congelamento e fingerprint')
        for row in previous['rows']:
            if row.get('execution_protocol')!=protocol:parser.error('protocolo da execução anterior divergente')
            if row.get('configuration')!=configuration(row['configuration_id']):parser.error('configuração anterior alterada')
            if sha256(Path(row['result_file']).read_bytes()).hexdigest()!=row.get('result_sha256'):
                parser.error('artefato anterior alterado')
            if row.get('quota_status')=='incomplete':
                prior_failed_rows.append(row)
            else:prior_rows.append(row)
    prior_keys=[(r['source_id'],r['configuration_id'],r['repetition'],r.get('audience','beginner'),r.get('output_format','analytical_text')) for r in prior_rows]
    if len(prior_keys)!=len(set(prior_keys)):parser.error('execuções anteriores duplicadas')
    from suno_asset.m7_literal_acceptance import COMBINATIONS
    combinations=sorted(COMBINATIONS) if args.matrix else [('beginner','analytical_text')]
    wanted=[(s['id'],n,r,a,f) for r in range(1,args.repeats+1) for s in sources for n in args.configurations for a,f in combinations]
    if set(prior_keys)-set(wanted):parser.error('rodada anterior fora do escopo')
    count=len(wanted)-len(prior_keys)
    ceiling=count*MAX_TOKENS*max(max(p) for p in PRICES.values())/1e6
    if quota_ledger:
        from suno_asset.m7_recovery import campaign_admission_cost
        jobs=[execution_identity(s,n,a,f,r,fingerprint_sha256,protocol)[1]
            for r in range(1,args.repeats+1) for s in sources for n in args.configurations for a,f in combinations]
        try:ceiling=campaign_admission_cost(quota_ledger,jobs,MAX_TOKENS*max(max(p) for p in PRICES.values())/1e6)
        except (ValueError,KeyError,TypeError,OSError) as exc:parser.error(str(exc))
    if not 0<=count<=args.max_runs or (count==0 and not prior_paths) or len(set(args.configurations))!=len(args.configurations):parser.error('contagem/configurações inválidas')
    if not 0<args.max_cost_usd<=10 or (not args.plan and ceiling>args.max_cost_usd):parser.error(f'teto conservador USD {ceiling:.3f} excede orçamento')
    from suno_asset.m7_acceptance import fingerprint
    report={'version':'architecture-comparison-v1','fingerprint':current_fingerprint,
        'execution_protocol':protocol,
        'configuration_specs':[configuration(n) for n in args.configurations],
        'max_refinements':args.max_refinements,'heldout_included':args.allow_heldout,'max_cost_usd':args.max_cost_usd,'conservative_ceiling_usd':ceiling,
        'paid':False,'provider_calls_real':not args.plan,'quota_mode':'simulated' if args.plan else 'free_only',
        'authorized_billing_usd':0,'matrix':args.matrix,'status':'running','started_at':datetime.now(timezone.utc).isoformat(),'rows':list(prior_rows),
        'prior_failed_results':prior_failed_rows,
        'stage_cost_caps_usd':stage_cost_limits,
        'budget':{'per_execution_ceiling_usd':MAX_TOKENS*max(max(p) for p in PRICES.values())/1e6,
            'round_ceiling_usd':ceiling,'includes_retries_and_refinements':True,'unknown_usage':'reserve full planned attempt cost; do not count as zero'}}
    if resume_report:
        if (resume_report.get('configuration_specs')!=report['configuration_specs']
            or resume_report.get('matrix')!=report['matrix']
            or resume_report.get('stage_cost_caps_usd')!=stage_cost_limits
            or resume_report.get('max_cost_usd')!=args.max_cost_usd):
            parser.error('retomada exige configurações e tetos idênticos do mesmo lote')
        report['started_at']=resume_report['started_at']
        report['resumed_at']=datetime.now(timezone.utc).isoformat()
    if args.allow_heldout and not args.resume_reserve:
        from suno_asset.m7_promotion import begin_reserve
        try: marker=begin_reserve(args.frozen,args.corpus,args.configurations[0],args.out)
        except (ValueError,KeyError,TypeError,OSError) as exc:parser.error(str(exc))
        report.update(frozen_sha256=sha256(args.frozen.read_bytes()).hexdigest(),reserve_ledger_sha256=sha256(marker.read_bytes()).hexdigest())
    elif args.resume_reserve:
        from suno_asset.m7_promotion import reserve_ledger_path
        marker=reserve_ledger_path(args.corpus)
        try:claim=json.loads(marker.read_text())
        except (OSError,ValueError) as exc:parser.error(f'registro de reserva ausente/inválido: {exc}')
        if (claim.get('status')!='started' or claim.get('correction_guided_by_reserve') is not False
            or claim.get('frozen_sha256')!=sha256(args.frozen.read_bytes()).hexdigest()
            or Path(claim.get('out','')).resolve()!=args.out.resolve()
            or resume_report.get('reserve_ledger_sha256')!=sha256(marker.read_bytes()).hexdigest()
            or marker.with_name(marker.stem+'-correction.json').exists()):
            parser.error('registro da reserva não permite retomada neste congelamento/lote')
        report.update(frozen_sha256=claim['frozen_sha256'],reserve_ledger_sha256=sha256(marker.read_bytes()).hexdigest())
    if not args.resume_reserve:args.out.mkdir(parents=True)
    _write_summary(args.out/'summary.json',report)
    failures={}
    stopped=False
    for repetition in range(1,args.repeats+1):
        for source in sources:
            for name in args.configurations:
                for audience,output_format in combinations:
                    if (source['id'],name,repetition,audience,output_format) in prior_keys:continue
                    out=args.out/f'repetition-{repetition}'
                    if args.matrix:out=out/audience/output_format
                    identity,quota_job_id=execution_identity(source,name,audience,output_format,repetition,fingerprint_sha256,protocol)
                    if quota_ledger:
                        try:register_execution(quota_ledger,identity,quota_job_id)
                        except ValueError as exc:parser.error(str(exc))
                        try:existing=quota_ledger.resolve_job(quota_job_id)
                        except (ValueError,KeyError,TypeError,OSError) as exc:parser.error(str(exc))
                        quota_job_id=existing['job_id']
                        if existing and existing['status']=='complete':
                            result_path=Path(existing['result_path'])
                            if (not result_path.is_file() or sha256(result_path.read_bytes()).hexdigest()!=existing['result_sha256']):
                                parser.error('resultado da tarefa gratuita no ledger alterado/ausente')
                            row=json.loads(result_path.read_text())
                            if (row.get('quota_job_id')!=quota_job_id
                                or row.get('quota_fingerprint_sha256')!=fingerprint_sha256
                                or row.get('source_id')!=source['id']
                                or row.get('configuration_id')!=name
                                or row.get('audience')!=audience or row.get('output_format')!=output_format
                                or row.get('repetition',repetition)!=repetition
                                or row.get('execution_protocol')!=protocol):
                                parser.error('resultado recuperado do ledger não corresponde à combinação solicitada')
                            row['repetition']=repetition
                            report['rows'].append(row)
                            _write_summary(args.out/'summary.json',report)
                            print(json.dumps({'source_id':source['id'],'configuration_id':name,'repetition':repetition,
                                'audience':audience,'output_format':output_format,'status':row.get('status'),
                                'quota_recovered_from_ledger':True},ensure_ascii=False),flush=True)
                            continue
                        if existing and existing['status']!='queued':
                            parser.error('combinação gratuita já iniciada sem resultado completo; não repetir chamadas incertas')
                    row=plan_case(source,name,out,audience=audience,output_format=output_format,max_refinements=args.max_refinements,
                        stage_cost_limits=stage_cost_limits) if args.plan else run_case(source,name,out,args.max_refinements,
                        audience=audience,output_format=output_format,stage_cost_limits=stage_cost_limits,free_only=args.free_only,
                        quota_job_id=quota_job_id,fingerprint_sha256=fingerprint_sha256,repetition=repetition,
                        execution_protocol=protocol)
                    if row.get('quota_status')=='deferred':
                        report['status']='quota_deferred'
                        _write_summary(args.out/'summary.json',report)
                        (args.out/'report.md').write_text(report_markdown(report))
                        print(json.dumps({'source_id':source['id'],'configuration_id':name,'repetition':repetition,
                            'audience':audience,'output_format':output_format,'status':'quota_deferred',
                            'next_window':quota_ledger.next_available_window(quota_job_id)},ensure_ascii=False),flush=True)
                        stopped=True
                        break
                    row['repetition']=repetition;report['rows'].append(row)
                    _save(args.out/f'progress-{len(report["rows"])}.json',report)
                    _write_summary(args.out/'summary.json',report)
                    print(json.dumps({k:row.get(k) for k in ('source_id','configuration_id','repetition','audience','output_format','status','attempts','errors')},ensure_ascii=False),flush=True)
                    for error in set(row['errors']) if not args.plan else ():
                        failures[error]=failures.get(error,0)+1
                        if failures[error]>=2:stopped=True
                    if stopped:break
                if stopped:break
            if stopped:break
        if stopped:break
    if report['status']!='quota_deferred':
        report['status']='stopped_recurring_technical_failure' if stopped else 'planned' if args.plan else 'completed_pending_review'
    if args.plan:
        from suno_asset.m7_budget import aggregate_round_budget
        report['offline_round_budget']=aggregate_round_budget(report['rows'],args.max_cost_usd,args.max_refinements)
    _write_summary(args.out/'summary.json',report)
    (args.out/'report.md').write_text(report_markdown(report))


if __name__=='__main__':main()
