"""Ensaio de correção numérica em ata completa, sem perder fatos críticos."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from decimal import Decimal

from suno_asset.domain.models import ExecutionResult
from suno_asset.m7_acceptance import fingerprint
from suno_asset.m7_literal_eval import configuration,cost_records
from suno_asset.m7_eval import PRICES,_save
from suno_asset.services.groq import GroqRefiner,MAX_TOKENS,make_session
from suno_asset.services.literal import LiteralEvaluator,LiteralGenerator,validate_extraction
from suno_asset.services.source_preparation import source_artifacts
from suno_asset.workflow.graph import LITERAL_POLICY,_no_regression
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.m7_cases import (_atomic_report, _task_identity, _start_persistent_task,
    _finish_persistent_task)


def plan_refinement(source,name,out):
    """Offline preflight for correction plus both complete evaluations."""
    from uuid import uuid4
    from suno_asset.domain.models import EvaluationIssue,GenerationRequest
    from suno_asset.m7_budget import PlanningSession,PlannedOnly
    from suno_asset.services.ingestion import register_document,parse_document
    from suno_asset.services.local_storage import LocalStorage
    from suno_asset.services.parsing import ManualParser
    from suno_asset.services.source_preparation import source_artifacts
    from suno_asset.services.literal import (LiteralExtractor,SimulatedLiteralExtractor,
        SimulatedLiteralGenerator,LiteralEvaluator,LiteralGenerator)
    from suno_asset.workflow.graph import LITERAL_POLICY

    if name not in {'literal-quality','literal-hybrid'}:
        raise ValueError('planejamento exige perfil literal')
    raw=Path(source['local_pdf']).read_bytes()
    if sha256(raw).hexdigest()!=source['sha256']:
        raise ValueError('hash da fonte divergente')
    store=LocalStorage(Path(out)/'artifacts')
    registered=store.put_pdf(raw,filename=Path(source['local_pdf']).name)
    document=register_document(store,registered)
    _,parsed=parse_document(store,ManualParser(),document)
    session=PlanningSession('offline-no-provider-call',transport=lambda *_:None,tpm_limit=8000,
        rpm_limit=30,rpd_limit=1000,tpd_limit=200000)
    spec=configuration(name);session.configure(spec['architecture'],spec['profile'])
    prepared=LiteralExtractor(session).prepare(parsed.units)
    _,evidence=source_artifacts(prepared)
    extraction=SimulatedLiteralExtractor().extract_prepared(prepared)
    request=GenerationRequest(request_id=uuid4(),source=registered,mode='real',
        architecture='literal-v1',model_profile=spec['profile'])
    content=SimulatedLiteralGenerator().generate(request,extraction.anchors,evidence)
    evaluator=LiteralEvaluator(session)
    evaluator.evaluate(content,extraction.anchors,evidence,LITERAL_POLICY,plan_only=True)
    refiner=GroqRefiner(LiteralGenerator(session))
    try:
        refiner.refine(content,(EvaluationIssue(code='controlled_numeric_error',
            message='Plano offline: correção numérica controlada com fonte disponível',correctable=True),),
            extraction.anchors,evidence,1)
    except PlannedOnly:
        pass
    revised=content.model_copy(update={'revision':1})
    evaluator.evaluate(revised,extraction.anchors,evidence,LITERAL_POLICY,plan_only=True)
    stages={}
    for plan in session.plans:
        stage=stages.setdefault(plan['stage'],{'reserved_tokens':0,'reserved_calls':0,
            'estimated_seconds':0.0,'requests':0,'per_request':[]})
        stage['reserved_tokens']+=plan['reserved_stage_tokens']+plan['retry_reserve']
        stage['reserved_calls']+=plan['requests']+1
        stage['estimated_seconds']+=plan['estimated_stage_seconds']
        stage['requests']+=plan['requests']
        stage['per_request'].extend(plan['budgets'])
    totals={key:sum(row[key] for row in stages.values()) for key in
        ('reserved_tokens','reserved_calls','estimated_seconds')}
    limits={'tokens':MAX_TOKENS,'calls':64,'seconds':2400,'tpm':8000}
    expected={'evaluate':2,'refine':1}
    complete=all(stage in stages and stages[stage]['requests']>0 for stage in expected)
    fits=(complete and totals['reserved_tokens']<=limits['tokens']
        and totals['reserved_calls']<=limits['calls'] and totals['estimated_seconds']<limits['seconds']
        and all(item['fits'] for row in stages.values() for item in row['per_request']))
    return {'version':'m7-full-refinement-offline-plan-v1','paid':False,'configuration':spec,
        'source_id':source['id'],'source_sha256':source['sha256'],'max_tokens':limits['tokens'],
        'max_calls':limits['calls'],'deadline_seconds':limits['seconds'],'tpm_limit':limits['tpm'],
        'stages':stages,'totals':totals,'complete_proxy_plan':complete,'proxy_fits':bool(fits),
        'actual_output_feasibility':'unknown',
        'limitation':'Rascunho e correção simulados somente para dimensionamento; nenhuma chamada/resultado semântico foi produzido.'}


def inject_numeric_error(content,anchors):
    for anchor in sorted(anchors,key=lambda a:a.kind.value!='decision'):
        for fact in anchor.numeric_facts:
            original=fact.original
            match=re.fullmatch(r'(\d+)([,.])(\d+)(%?)',original)
            if not match:continue
            old=Decimal(match.group(1)+'.'+match.group(3))
            changed=f'{old+1:.{len(match.group(3))}f}'.replace('.',match.group(2))+match.group(4)
            for claim in content.claims:
                if anchor.anchor_id not in claim.anchor_ids or original not in claim.text:continue
                changed_text=claim.text.replace(original,changed,1)
                claims=tuple(c.model_copy(update={'text':changed_text}) if c.text==claim.text else c for c in content.claims)
                updates={'revision':0,'claims':claims}
                for section in ('synthesis','development','implications'):
                    updates[section]=getattr(content,section).replace(claim.text,changed_text,1)
                return content.model_copy(update=updates),{'anchor_id':anchor.anchor_id,'claim_id':claim.claim_id,'original':original,'injected':changed}
    raise ExpectedServiceError('refinement_input_unavailable','nenhum fato numérico explícito apropriado ao ensaio')


def run_refinement(base_row,source,out,*,session=None,stage_cost_limits=None,fingerprint_sha256=None):
    spec=base_row['configuration']
    if spec!=configuration(base_row['configuration_id']) or spec['architecture']!='literal-v1':
        raise ValueError('ensaio exige candidato literal com perfil completo')
    path=Path(base_row['result_file'])
    if sha256(path.read_bytes()).hexdigest()!=base_row['result_sha256']:
        raise ValueError('artefato inicial alterado')
    if sha256(Path(source['local_pdf']).read_bytes()).hexdigest()!=base_row['source_sha256'] or source['sha256']!=base_row['source_sha256']:
        raise ValueError('fonte alterada')
    base=ExecutionResult.model_validate_json(path.read_bytes())
    if not base.content or not base.prepared_source or not base.extraction or not base.fact_verification or not base.fact_verification.approved:
        raise ValueError('ata completa com fatos verificados ainda indisponível')
    validate_extraction(base.prepared_source,base.extraction)
    before,injected=inject_numeric_error(base.content,base.extraction.anchors)
    _,evidence=source_artifacts(base.prepared_source)
    if not fingerprint_sha256:
        raise ValueError('ensaio real exige fingerprint persistente explícito')
    identity,job_id=_task_identity('full-ata-refinement',out,fingerprint_sha256,
        source_id=source['id'],source_sha256=source['sha256'],configuration_id=base_row['configuration_id'],
        base_result_sha256=base_row['result_sha256'],stage_cost_limits=dict(stage_cost_limits or {}))
    session=session or make_session(free_only=True)
    recovered=_start_persistent_task(session,identity,job_id)
    if recovered is not None:return recovered
    session.stage_cost_limits=dict(stage_cost_limits or {})
    session.configure(spec['architecture'],spec['profile'])
    from uuid import uuid4
    from suno_asset.m7_literal_eval import audited_transport
    audit_root=out/'runs'/session.quota_job_id
    original_transport=session.transport
    session.transport=audited_transport(session,audit_root)
    evaluator=LiteralEvaluator(session);refiner=GroqRefiner(LiteralGenerator(session))
    before_report=after=after_report=None
    error=None
    try:
        before_report=evaluator.evaluate(before,base.extraction.anchors,evidence,LITERAL_POLICY)
        if before_report.approved:
            raise ExpectedServiceError('known_critical_error_approved','erro numérico controlado aprovado')
        after=refiner.refine(before,before_report.issues,base.extraction.anchors,evidence,1)
        after_report=evaluator.evaluate(after,base.extraction.anchors,evidence,LITERAL_POLICY)
    except ExpectedServiceError as exc:error=exc.code
    finally:session.transport=original_transport
    success=bool(before_report and not before_report.approved and after_report and after_report.approved
        and _no_regression(before_report,after_report) and all(f.result.value=='passed' for f in after_report.critical_facts))
    value={'version':'full-ata-refinement-v1','configuration':spec,'configuration_id':base_row['configuration_id'],
        'source_id':source['id'],'source_sha256':source['sha256'],'base_result_file':str(path.resolve()),
        'base_result_sha256':base_row['result_sha256'],'injected_error':injected,'automatic_passed':success,
        'error':error,'human_review_status':'pending','before':before.model_dump(mode='json'),
        'before_evaluation':before_report.model_dump(mode='json') if before_report else None,
        'after':after.model_dump(mode='json') if after else None,
        'after_evaluation':after_report.model_dump(mode='json') if after_report else None,
        'per_stage':cost_records(session),'attempts':session.attempt_records,'manifest':session.manifest(),
        'audit_dir':str((audit_root/'call-audit').resolve()),
        'audit_hashes':{str(p.resolve()):sha256(p.read_bytes()).hexdigest() for p in (audit_root/'call-audit').rglob('*.json')},
        'limitation':'Erro controlado em texto de ata completa; ganho/preservação exigem revisão independente por fato.'}
    return _finish_persistent_task(session,identity,job_id,value,
        out/f'{source["id"]}-{base_row["configuration_id"]}-refinement.json',
        error=error,complete=bool(before_report and after and after_report))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-summary',type=Path,required=True)
    parser.add_argument('--corpus',type=Path,default=Path('tests/fixtures/evaluation/corpus.json'))
    parser.add_argument('--configurations',nargs='+',choices=('literal-quality','literal-hybrid'),default=['literal-quality','literal-hybrid'])
    parser.add_argument('--source-id',default='copom281-20260916')
    parser.add_argument('--max-cost-usd',type=float,required=True)
    parser.add_argument('--evaluate-stage-cost-usd',type=float,required=True)
    parser.add_argument('--refine-stage-cost-usd',type=float,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--resume',action='store_true',help='Retomar somente ensaios ainda não iniciados')
    args=parser.parse_args()
    if args.resume and not (args.out/'batch.json').is_file():parser.error('--resume exige lote persistente existente')
    if args.out.exists() and not args.resume:parser.error('--out exige pasta nova')
    summary=json.loads(args.base_summary.read_text())
    if summary.get('fingerprint')!=fingerprint(args.corpus):parser.error('rodada pertence a outra arquitetura/versão')
    source=next((s for s in json.loads(args.corpus.read_text())['sources'] if s['id']==args.source_id and s['split']=='development'),None)
    if source is None:parser.error('fonte desconhecida/reservada')
    ceiling=len(args.configurations)*MAX_TOKENS*max(max(p) for p in PRICES.values())/1e6
    if (not 0<args.max_cost_usd<=10 or ceiling>args.max_cost_usd
        or not 0<args.evaluate_stage_cost_usd<=args.max_cost_usd
        or not 0<args.refine_stage_cost_usd<=args.max_cost_usd):
        parser.error(f'teto conservador USD {ceiling:.3f} excede orçamento/limite por etapa')
    rows=[]
    for name in args.configurations:
        candidates=[r for r in summary['rows'] if r.get('configuration_id')==name and r['source_id']==source['id'] and r.get('technical_complete')]
        if not candidates:parser.error('nenhuma ata completa disponível para '+name)
        from suno_asset.m7_acceptance import validate_paid_execution
        try:validate_paid_execution(candidates[0],expected_configuration=configuration(name))
        except (ValueError,KeyError,TypeError,OSError) as exc:parser.error(str(exc))
        rows.append(candidates[0])
    try:
        admission_session=make_session(free_only=True)
        admission_session.require_free_quota_evidence()
    except ExpectedServiceError as exc:parser.error(str(exc))
    stage_cost_limits={'evaluate':args.evaluate_stage_cost_usd,'refine':args.refine_stage_cost_usd}
    batch={'fingerprint':fingerprint(args.corpus),'out':str(args.out.resolve()),
        'base_summary_sha256':sha256(args.base_summary.read_bytes()).hexdigest(),
        'source_sha256':source['sha256'],'source_id':source['id'],'configurations':args.configurations,
        'max_cost_usd':args.max_cost_usd,'stage_cost_limits':stage_cost_limits}
    if args.resume:
        if json.loads((args.out/'batch.json').read_text())!=batch:parser.error('identidade/hashes do lote divergentes')
    from suno_asset.m7_recovery import campaign_admission_cost
    jobs=[_task_identity('full-ata-refinement',args.out,batch['fingerprint'],
        source_id=source['id'],source_sha256=source['sha256'],configuration_id=row['configuration_id'],
        base_result_sha256=row['result_sha256'],stage_cost_limits=stage_cost_limits)[1] for row in rows]
    try:ceiling=campaign_admission_cost(admission_session.quota_ledger,jobs,
        MAX_TOKENS*max(max(p) for p in PRICES.values())/1e6)
    except (ValueError,KeyError,TypeError,OSError) as exc:parser.error(str(exc))
    if ceiling>args.max_cost_usd:
        parser.error(f'teto agregado com predecessoras USD {ceiling:.8f} excede orçamento')
    if not args.resume:_save(args.out/'batch.json',batch)
    results=[];deferred=None
    for row in rows:
        try:result=run_refinement(row,source,args.out,stage_cost_limits=stage_cost_limits,
            fingerprint_sha256=batch['fingerprint'])
        except (ValueError,ExpectedServiceError) as exc:parser.error(str(exc))
        if result['quota_status']=='deferred':deferred=result;break
        results.append(result)
        _atomic_report(args.out/'progress.json',{'batch':batch,'rows':results})
        if result['quota_status']=='incomplete':
            deferred={'quota_status':'incomplete','quota_job_id':result['quota_job_id']};break
    _atomic_report(args.out/'summary.json',{'version':'full-ata-refinement-v1',
        'fingerprint':batch['fingerprint'],'paid':False,'provider_calls_real':True,'quota_mode':'free_only',
        'authorized_billing_usd':0,'batch':batch,'status':deferred['quota_status'] if deferred else 'complete',
        'deferred':deferred,'rows':results,'max_cost_usd':args.max_cost_usd,'human_review_status':'pending'})


if __name__=='__main__':main()
