"""Congela arquitetura e perfil completos, após revisão independente de 18 runs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from suno_asset.m7_acceptance import (digest,fingerprint,prepare_outputs,validate_outputs,validate_account,identified,
    validate_trace_hashes,validate_paid_execution,technical_attempts_complete,provider_calls_are_real,
    validate_execution_protocol)
from suno_asset.m7_literal_eval import CONFIGURATIONS,configuration
from suno_asset.m7_review import validate_review, validate_codex_identity, codex_metadata, validate_inventories
from suno_asset.m7_eval import _save


def freeze_configuration_v2(name,form_path,account_path,outputs_path,corpus_path,cases_path,refinements_path=None):
    if name not in {'literal-quality','literal-hybrid'}:raise ValueError('promover somente candidato literal')
    review=validate_review(json.loads(form_path.read_text()),corpus_path.parent)
    if not review['glossary_accepted']:raise ValueError('glossário não aceito')
    validate_account(json.loads(account_path.read_text()))
    quality=validate_outputs(json.loads(outputs_path.read_text()),corpus_path)
    if quality['run_fingerprint']!=fingerprint(corpus_path):raise ValueError('rodada pertence a outra arquitetura/versão')
    sources={s['id'] for s in json.loads(corpus_path.read_text())['sources'] if s['split']=='development'}
    rows=quality['run_rows']
    for candidate in CONFIGURATIONS:
        selected=[r for r in rows if r.get('configuration_id')==candidate]
        identities={(r['source_id'],r['repetition']) for r in selected}
        if len(selected)!=6 or identities!={(s,n) for s in sources for n in (1,2,3)}:
            raise ValueError('comparação exige seis execuções novas por arquitetura/perfil, 18 no total')
        for row in selected:
            if row.get('configuration')!=configuration(candidate):raise ValueError('perfil/modelos alterados na rodada')
    runs=[r for r in rows if r.get('configuration_id')==name]
    if any(not r.get('technical_complete') or r['errors'] or r['status']=='failed' for r in runs):
        raise ValueError('candidato com falha técnica ou execução incompleta')
    decisions=[r for r in quality['decisions'] if r.get('configuration_id')==name]
    if any(not r['human_passed'] for r in decisions if r['selected']):
        raise ValueError('todas as seis revisões entregues exigem preservação crítica e suporte independente')
    if any(r['automatic_approved'] and not r['human_passed'] for r in decisions):
        raise ValueError('falsa aprovação de erro crítico/conhecido')
    if {r['id'] for r in decisions}&set(quality['refinement_regressions']):
        raise ValueError('regressão crítica em refinamento')
    selected=[r for r in decisions if r['selected']]
    if not any(after['revision']>0 and after['human_passed'] and any(
        before['result_sha256']==after['result_sha256'] and before['revision']<after['revision'] and not before['human_passed']
        for before in decisions) for after in selected):
        if refinements_path is None:raise ValueError('ensaio de correção e preservação em ata completa pendente')
    if refinements_path is not None:
        validate_refinement_review(json.loads(refinements_path.read_text()),corpus_path)
    cases=json.loads(cases_path.read_text())
    if cases.get('fingerprint')!=fingerprint(corpus_path) or cases.get('review_form_sha256')!=digest(form_path):
        raise ValueError('casos de outra política/revisão')
    expected_ids={r['id'] for r in review['cases'] if r['split']=='development'}
    case_rows=[r for r in cases['rows'] if r.get('configuration_id')==name]
    if not case_rows or {r['id'] for r in case_rows}!=expected_ids or any(
        r['error'] or not r['prediction'] or (r['prediction']['factual_verdict']=='passed' and r['human_factual']!='passed') for r in case_rows):
        raise ValueError('casos incompletos ou falsa aprovação factual')
    return {'version':'m7-freeze-v2','status':'frozen','configuration':configuration(name),
        'fingerprint':fingerprint(corpus_path),
        'review_inputs':{k:str(p.resolve()) for k,p in {'form':form_path,'account':account_path,'outputs':outputs_path,'cases':cases_path,**({'refinements':refinements_path} if refinements_path else {})}.items()},
        'review_artifacts':{str(p.resolve()):digest(p) for p in (form_path,account_path,outputs_path,cases_path,*((refinements_path,) if refinements_path else ()))},
        'criteria':{'complete_candidate_runs':6,'comparison_runs':18,'critical_false_approvals':0,'unsupported_delivered_claims':0,'critical_refinement_regressions':0},
        'limitation':'Congelamento libera avaliação da reserva. Não altera o padrão nem conclui M7 automaticamente.'}


def validate_frozen_configuration_v2(path,corpus_path,name):
    frozen=json.loads(path.read_text())
    if (frozen.get('version')!='m7-freeze-v2' or frozen.get('status')!='frozen'
        or frozen.get('configuration')!=configuration(name) or frozen.get('fingerprint')!=fingerprint(corpus_path)):
        raise ValueError('arquitetura/perfil não congelados ou código mudou')
    if len(frozen.get('review_artifacts',{})) not in {4,5}:raise ValueError('revisões ausentes')
    for artifact,expected in frozen['review_artifacts'].items():
        if digest(artifact)!=expected:raise ValueError('revisão alterada após congelamento')
    args=frozen['review_inputs']
    if freeze_configuration_v2(name,Path(args['form']),Path(args['account']),Path(args['outputs']),corpus_path,Path(args['cases']),Path(args['refinements']) if args.get('refinements') else None)!=frozen:
        raise ValueError('critérios de congelamento não satisfeitos')
    return frozen



def prepare_refinement_review(summary_path, *, automated=False):
    summary=json.loads(summary_path.read_text())
    for run in summary['rows']: validate_trace_hashes(run)
    rows=[]
    for run in summary['rows']:
        if not run.get('after_evaluation'):raise ValueError('ensaio sem avaliação completa')
        rows.append({'configuration_id':run['configuration_id'],'source_id':run['source_id'],
            'correction_verified':None,'unsupported_after_claims':None,
            **({'material_facts':[], 'independent_metrics':{'numeric_errors':None,'attribution_errors':None},
                'rejection_reason':None} if automated else {}),
            'facts':[{'anchor_id':f['anchor_id'],'before':None,'after':None,'source_reference':None,'reason':None}
                for f in run['after_evaluation']['critical_facts']]})
    return {'version':'m7-codex-refinement-review-v3' if automated else 'full-refinement-review-v1','status':'pending',
        **(codex_metadata() if automated else {'reviewer':None}), 'reviewed_at':None,
        **({'inventories':{}} if automated else {}),
        'summary_file':str(summary_path.resolve()),'summary_sha256':digest(summary_path),'rows':rows}


def validate_refinement_review(form,corpus_path, *, candidate=None, base_runs=None):
    identified(form,'reviewed_at')
    summary_path=Path(form['summary_file'])
    if digest(summary_path)!=form['summary_sha256']:raise ValueError('ensaio alterado desde a preparação')
    summary=json.loads(summary_path.read_text())
    if summary.get('fingerprint')!=fingerprint(corpus_path):raise ValueError('ensaio de outra arquitetura')
    if candidate:
        sources={s['id']:s['sha256'] for s in json.loads(corpus_path.read_text())['sources'] if s['split']=='development'}
        selected_runs=[r for r in summary['rows'] if r.get('configuration_id')==candidate]
        if not provider_calls_are_real(summary) or not selected_runs or any(
            r.get('source_id') not in sources or r.get('source_sha256')!=sources[r['source_id']]
            or r.get('error') or not r.get('before') or not r.get('after') or not r.get('before_evaluation')
            or not r.get('after_evaluation') or not r.get('attempts')
            or not technical_attempts_complete(r)
            or not {'evaluate','refine'} <= {a.get('stage') for a in r.get('attempts',[])}
            or r['before_evaluation'].get('approved') is not False
            or r['after_evaluation'].get('approved') is not True for r in selected_runs):
            raise ValueError('Refinamento real em ata completa do candidato ainda não comprovado')
        if any(r.get('configuration') != configuration(candidate) or r['after'].get('revision',0) <= r['before'].get('revision',0)
            or not any(a.get('stage')=='refine' and a.get('status')=='complete' for a in r['attempts']) for r in selected_runs):
            raise ValueError('Chamada/revisão de refinamento real não comprovada')
        for run in selected_runs:
            from suno_asset.services.groq import LITERAL_PROMPT_VERSION
            if (run.get('manifest') or {}).get('prompt_version') != LITERAL_PROMPT_VERSION:
                raise ValueError('Refinamento de qualificação exige versão atual do prompt')
            bases = [base for base in (base_runs or [])
                if base.get('configuration_id') == candidate and base.get('source_id') == run.get('source_id')
                and base.get('source_sha256') == run.get('source_sha256')
                and base.get('result_file') == run.get('base_result_file')
                and base.get('result_sha256') == run.get('base_result_sha256')]
            if len(bases) != 1:
                raise ValueError('Refinamento source-first sem vínculo à execução base auditada')
            base = validate_paid_execution(bases[0],expected_configuration=configuration(candidate),require_current_prompt=True)
            validate_refinement_execution(run, base, configuration(candidate))
        inventory_ids=validate_inventories(form,{r['source_id']:r['source_sha256'] for r in summary['rows']})
    expected=prepare_refinement_review(summary_path)
    expected_rows={(r['configuration_id'],r['source_id']):r for r in expected['rows']}
    rows=form.get('rows') or []
    if len(rows)!=len(expected_rows) or {(r['configuration_id'],r['source_id']) for r in rows}!=set(expected_rows):
        raise ValueError('ensaios ausentes ou duplicados')
    if not candidate and {r['configuration_id'] for r in rows}!={'literal-quality','literal-hybrid'}:
        raise ValueError('ensaio completo exigido para ambos os perfis candidatos')
    states={'preserved','omitted','altered','not_verified'}
    for row in rows:
        if candidate and row['configuration_id'] != candidate:
            if not row.get('rejection_reason'):
                raise ValueError('Resultado/motivo de candidato rejeitado ausente')
            continue
        original=expected_rows[row['configuration_id'],row['source_id']]
        facts=row.get('facts') or []
        if candidate:
            material=row.get('material_facts') or []
            if (len(material)!=len(inventory_ids[row['source_id']]) or {f['id'] for f in material}!=inventory_ids[row['source_id']]
                or any(f.get('after')!='preserved' or f.get('before') not in states or not f.get('source_reference')
                    or not f.get('support_excerpt') or not f.get('reason') for f in material)
                or any(type(row.get('independent_metrics',{}).get(k)) is not int or row['independent_metrics'][k]!=0
                    for k in ('numeric_errors','attribution_errors'))):
                raise ValueError('Preservação de todo inventário material/suporte/atribuição no refinamento pendente')
        if (len(facts)!=len(original['facts']) or {f['anchor_id'] for f in facts}!={f['anchor_id'] for f in original['facts']}
            or row.get('correction_verified') is not True or type(row.get('unsupported_after_claims')) is not int
            or row['unsupported_after_claims']!=0):
            raise ValueError('correção/cobertura/suporte independente pendentes')
        for fact in facts:
            if fact.get('before') not in states or fact.get('after')!='preserved' or not fact.get('reason') or not fact.get('source_reference'):
                raise ValueError('regressão ou revisão por fato crítico pendente')
        if not any(f['before']=='altered' for f in facts):raise ValueError('ganho da correção não confirmado')
    return form


def validate_refinement_execution(run, base, spec):
    """Rebuild the full correction and both evaluations from audited replies."""
    from suno_asset.m7_acceptance import audit_replay_session
    from suno_asset.m7_literal_refinement import inject_numeric_error
    from suno_asset.services.groq import GroqRefiner
    from suno_asset.services.literal import LiteralEvaluator, LiteralGenerator
    from suno_asset.services.source_preparation import source_artifacts
    from suno_asset.services.protocols import ExpectedServiceError
    from suno_asset.workflow.graph import LITERAL_POLICY, _no_regression
    if not base.content or not base.extraction or not base.prepared_source:
        raise ValueError('Base completa do refinamento ausente')
    session = audit_replay_session(run, spec)
    try:
        before, injected = inject_numeric_error(base.content, base.extraction.anchors)
        if before.model_dump(mode='json') != run.get('before') or injected != run.get('injected_error'):
            raise ValueError('Perturbação controlada diverge da base auditada')
        _, evidence = source_artifacts(base.prepared_source)
        evaluator = LiteralEvaluator(session)
        before_report = evaluator.evaluate(before, base.extraction.anchors, evidence, LITERAL_POLICY)
        after = GroqRefiner(LiteralGenerator(session)).refine(
            before, before_report.issues, base.extraction.anchors, evidence, 1)
        after_report = evaluator.evaluate(after, base.extraction.anchors, evidence, LITERAL_POLICY)
    except ExpectedServiceError as exc:
        raise ValueError('Refinamento sem controles completos reconstruídos da auditoria') from exc
    session.assert_exhausted()
    if (before_report.model_dump(mode='json') != run.get('before_evaluation')
        or after.model_dump(mode='json', exclude={'content_id'}) != {
            key:value for key,value in (run.get('after') or {}).items() if key != 'content_id'}
        or after_report.model_dump(mode='json') != run.get('after_evaluation')
        or before_report.approved or not after_report.approved
        or not _no_regression(before_report, after_report)):
        raise ValueError('Revisão/pareceres do refinamento divergem das respostas auditadas')
    return after_report


def freeze_configuration(name, form_path, account_path, outputs_path, corpus_path, cases_path,
                         refinements_path=None, matrix_path=None, candidate_reviews_path=None):
    """New freezes exclusively accept independently completed Codex v3 records."""
    review = validate_review(json.loads(form_path.read_text()), corpus_path.parent)
    if review.get('version') != 'm7-codex-review-v3' or review.get('split') != 'development' or len(review['cases']) != 51:
        raise ValueError('Congelamento v3 exige revisão Codex dos 51 casos de desenvolvimento')
    if not review['glossary_accepted']:
        raise ValueError('Glossário não aceito')
    account = json.loads(account_path.read_text())
    if account.get('version') != 'm7-public-account-v3':
        raise ValueError('Registro da conta v3 obrigatório')
    validate_account(account)
    output_form = json.loads(outputs_path.read_text())
    if output_form.get('version') != 'm7-codex-output-review-v3':
        raise ValueError('Revisão das saídas Codex v3 obrigatória')
    quality = validate_outputs(output_form, corpus_path)
    if quality['run_fingerprint'] != fingerprint(corpus_path):
        raise ValueError('Código/prompts/rubricas alterados')
    corpus = json.loads(corpus_path.read_text())
    sources = {s['id'] for s in corpus['sources'] if s['split'] == 'development'}
    if len(sources) != 2 or len(quality['run_rows']) != 18:
        raise ValueError('Comparação exige 18 execuções principais nas duas atas')
    main_protocol = None
    for run in quality['run_rows']:
        validate_execution_protocol(run, expected_protocol=main_protocol, purpose='main')
        main_protocol = run['execution_protocol']
    cases = json.loads(cases_path.read_text())
    if not provider_calls_are_real(cases) or cases.get('fingerprint') != fingerprint(corpus_path) or cases.get('review_form_sha256') != digest(form_path):
        raise ValueError('Casos de outra política/revisão')
    expected_ids = {r['id'] for r in review['cases']}
    labels = {r['id']:r for r in review['cases']}
    from suno_asset.m7_cases import source_pages
    from suno_asset.m7_acceptance import validate_case_execution
    development_sources = [s for s in corpus['sources'] if s['split'] == 'development']
    case_proposals = {case['id']:case for source in development_sources
        for case in json.loads((corpus_path.parent/source['case_file']).read_text())}
    pages_by_source = {source['id']:source_pages(source) for source in development_sources}
    reasons, ranks = {}, {}
    for candidate in CONFIGURATIONS:
        runs = [r for r in quality['run_rows'] if r.get('configuration_id') == candidate]
        if (len(runs) != 6 or {(r['source_id'],r['repetition']) for r in runs} != {(s,n) for s in sources for n in (1,2,3)}
            or any(r.get('configuration') != configuration(candidate) for r in runs)):
            raise ValueError('Comparação incompleta ou configurações alteradas')
        case_rows = [r for r in cases['rows'] if r.get('configuration_id') == candidate]
        for row in case_rows:
            validate_trace_hashes(row)
            validate_case_execution(row, case_proposals[row['id']], pages_by_source[row['source_id']], configuration(candidate))
        if len(case_rows) != 51 or {r['id'] for r in case_rows} != expected_ids:
            raise ValueError('51 casos exigidos por configuração')
        if any(r.get('human_factual') != labels[r['id']]['factual_verdict']
            or r.get('human_audience') != labels[r['id']]['audience_verdict']
            or r.get('source_id') != labels[r['id']]['source_id']
            or r.get('configuration') != configuration(candidate) for r in case_rows):
            raise ValueError('Rótulos/configuração dos casos alterados após revisão Codex')
        decisions = [r for r in quality['decisions'] if r.get('configuration_id') == candidate]
        selected = [r for r in decisions if r['selected']]
        failures = []
        if candidate == 'current-quality': failures.append('arquitetura histórica sem matriz literal')
        for run in runs:
            try:
                result=validate_paid_execution(run,require_current_prompt=True)
                if run.get('errors') or result.status.value == 'failed':
                    raise ValueError('resultado contém falha técnica')
            except (ValueError,TypeError,KeyError,OSError) as exc:
                failures.append(f'execução tecnicamente incompleta: {exc}')
                break
        if len(selected) != 6 or any(not r['human_passed'] for r in selected): failures.append('saídas selecionadas reprovadas')
        if any(r['automatic_approved'] and not r['human_passed'] for r in decisions): failures.append('falsa aprovação conhecida')
        if any(r['independent_metrics']['false_approvals'] for r in decisions): failures.append('falsa aprovação factual contada pelo revisor')
        if {r['id'] for r in decisions} & set(quality['refinement_regressions']): failures.append('regressão crítica')
        if any(r.get('error') or not r.get('prediction') or (
            r['prediction']['factual_verdict'] == 'passed' and r['human_factual'] != 'passed')
            or not technical_attempts_complete(r) for r in case_rows):
            failures.append('falha/falsa aprovação factual nos casos')
        reasons[candidate] = failures
        if not failures:
            factual = sum(r['prediction']['factual_verdict'] != r['human_factual'] for r in case_rows) + sum(
                r['critical_error'] or any(r['criteria'][k]['verdict']!='passed' for k in ('coverage','support','numbers','citations'))
                or any(r['independent_metrics'][k] for k in ('unsupported_claims','numeric_errors','attribution_errors')) for r in decisions)
            editorial = sum(r['prediction'].get('audience_verdict') != r.get('human_audience') for r in case_rows) + sum(
                r['criteria']['adequacy']['verdict']!='passed' or r['format_verdict']!='passed' for r in decisions)
            from suno_asset.m7_eval import PRICES
            from suno_asset.m7_recovery import recovery_cost
            from suno_asset.services.groq import MAX_TOKENS
            conservative = MAX_TOKENS * max(max(p) for p in PRICES.values()) / 1e6
            ranks[candidate] = (factual, editorial,
                sum((round(r['estimated_usd'],6) if r.get('usage_complete') else conservative)
                    + recovery_cost(r) for r in runs), sum(r['wall_seconds'] for r in runs))
    if candidate_reviews_path is None:
        raise ValueError('Arquivo de pareceres de matriz/refinamento por candidato obrigatório')
    declared=json.loads(candidate_reviews_path.read_text())
    if set(declared) != {'literal-quality','literal-hybrid'}:
        raise ValueError('Pareceres de matriz e refinamento exigidos para ambos os candidatos literais')
    review_files={}
    for candidate,items in declared.items():
        if set(items) != {'refinements','matrix'}:
            raise ValueError('Cada candidato exige os caminhos de refinamento e matriz')
        review_files[candidate]={}
        for key,value in items.items():
            path=Path(value)
            if not path.is_absolute(): path=(candidate_reviews_path.parent/path).resolve()
            review_files[candidate][key]=path
    eligible_ranks={}
    for candidate in ('literal-quality','literal-hybrid'):
        candidate_paths=review_files.get(candidate)
        if not candidate_paths:
            reasons.setdefault(candidate,[]).append('matriz/refinamento do candidato pendentes')
            continue
        try:
            refinements=json.loads(candidate_paths['refinements'].read_text())
            if refinements.get('version') != 'm7-codex-refinement-review-v3':
                raise ValueError('Revisão de refinamento Codex v3 obrigatória')
            validate_refinement_review(refinements, corpus_path, candidate=candidate, base_runs=quality['run_rows'])
            validate_matrix_review(json.loads(candidate_paths['matrix'].read_text()), corpus_path, candidate,
                split='development', base_runs=quality['run_rows'])
            if candidate in ranks: eligible_ranks[candidate]=ranks[candidate]
        except (ValueError,TypeError,KeyError,AttributeError,OSError) as exc:
            reasons.setdefault(candidate,[]).append(f'matriz/refinamento reprovados: {exc}')
    if not eligible_ranks:
        raise ValueError('Nenhum candidato elegível: '+json.dumps(reasons,ensure_ascii=False,sort_keys=True))
    if name != min(eligible_ranks, key=lambda c: (eligible_ranks[c], c)):
        raise ValueError('candidato não vence seleção documentada')
    paths={'form':form_path,'account':account_path,'outputs':outputs_path,'cases':cases_path}
    paths['candidate_reviews']=candidate_reviews_path
    paths.update({f'{candidate}_{key}':path for candidate,items in review_files.items() for key,path in items.items()})
    return {'version':'m7-freeze-v4', 'status':'frozen', 'review_type':'codex',
        'configuration':configuration(name), 'fingerprint':fingerprint(corpus_path),
        'review_inputs': {k:str(p.resolve()) for k,p in paths.items()},
        'review_artifacts': {str(p.resolve()):digest(p) for p in paths.values()},
        'selection': {'ranks':{k:list(v) for k,v in ranks.items()},'eligible_ranks':{k:list(v) for k,v in eligible_ranks.items()}, 'rejected':reasons,
            'unknown_cost_basis':'conservative full execution token ceiling; unknown never equals zero'},
        'criteria':{'comparison_runs':18,'candidate_complete_approved_runs':6,'development_cases':51,
            'matrix_runs':18,'false_factual_approvals':0,'material_selected_errors':0,'critical_regressions':0}}


def validate_frozen_configuration(path, corpus_path, name):
    frozen = json.loads(path.read_text())
    if (frozen.get('version') != 'm7-freeze-v4' or frozen.get('status') != 'frozen'
        or frozen.get('configuration') != configuration(name) or frozen.get('fingerprint') != fingerprint(corpus_path)):
        raise ValueError('Congelamento automatizado v3 inválido ou código mudou')
    if len(frozen.get('review_artifacts', {})) != 9:
        raise ValueError('Artefatos de revisão v3 ausentes')
    for artifact, expected in frozen['review_artifacts'].items():
        if digest(artifact) != expected: raise ValueError('Revisão alterada após congelamento')
    args = frozen['review_inputs']
    fresh=freeze_configuration(name,*(Path(args[k]) for k in ('form','account','outputs')),
        corpus_path,Path(args['cases']),candidate_reviews_path=Path(args['candidate_reviews']))
    if fresh != frozen: raise ValueError('Critérios de congelamento não satisfeitos')
    return frozen


COMBINATIONS = {(audience, fmt) for audience in ('beginner','intermediate','advanced')
    for fmt in ('analytical_text','informational_carousel','short_video_script')}


def prepare_matrix_review(summary_path):
    summary = json.loads(summary_path.read_text())
    return {'version':'m7-codex-matrix-review-v3', 'status':'pending', **codex_metadata(), 'reviewed_at':None,
        'inventories':{},
        'summary_file':str(summary_path.resolve()), 'summary_sha256':digest(summary_path),
        'rows':[{key:r.get(key) for key in ('source_id','configuration_id','audience','output_format','result_file','result_sha256')}
            | {'verdict':None,'reason':None,'unsupported_claims':None,'numeric_errors':None,'attribution_errors':None,
               'false_approvals':None,'false_rejections':None,'facts':[], 'format_noncompliant_justified':None,
               'divergence_resolution':None} for r in summary['rows']]}


def validate_matrix_review(form, corpus_path, name, *, split, base_runs=None):
    validate_codex_identity(form)
    if form.get('version') != 'm7-codex-matrix-review-v3': raise ValueError('Revisão da matriz v3 obrigatória')
    path = Path(form['summary_file'])
    if digest(path) != form['summary_sha256']: raise ValueError('Matriz alterada')
    summary = json.loads(path.read_text())
    if summary.get('matrix') is not True:
        raise ValueError('Matriz exige lote explicitamente identificado como matriz')
    matrix_protocol = summary.get('execution_protocol')
    if not isinstance(matrix_protocol, dict):
        raise ValueError('Protocolo da matriz obrigatório')
    if summary.get('fingerprint') != fingerprint(corpus_path) or not provider_calls_are_real(summary):
        raise ValueError('Matriz exige execuções reais na versão congelada')
    sources = {s['id'] for s in json.loads(corpus_path.read_text())['sources'] if s['split']==split}
    source_hashes = {s['id']:s['sha256'] for s in json.loads(corpus_path.read_text())['sources'] if s['split']==split}
    inventory_ids = validate_inventories(form, source_hashes)
    expected = {(s,a,f) for s in sources for a,f in COMBINATIONS}
    rows, runs = form.get('rows') or [], summary['rows']
    if split == 'development' and base_runs is not None:
        def identities(run):
            from uuid import UUID
            result_path = Path(run['result_file']).resolve()
            result = json.loads(result_path.read_text())
            return {'quota_job_id': run.get('quota_job_id'),
                'result_file': str(result_path), 'result_sha256': digest(result_path),
                'request_id': str(UUID(str(result['request_id'])))}
        main_identities = [identities(run) for run in base_runs]
        for run in runs:
            current = identities(run)
            if any(value and any(value == original.get(field) for original in main_identities)
                for field, value in current.items()):
                raise ValueError('Matriz deve conter execuções adicionais, sem reutilizar execuções principais')
    if len(rows) != len(expected) or len(runs) != len(expected): raise ValueError('Matriz incompleta')
    key = lambda r: (r['source_id'],r.get('audience'),r.get('output_format'))
    if {key(r) for r in rows} != expected or {key(r) for r in runs} != expected: raise ValueError('Nove combinações exigidas por ata')
    by_key = {key(r):r for r in runs}
    for row in rows:
        run = by_key[key(row)]
        validate_execution_protocol(run, expected_protocol=matrix_protocol,
            purpose='development-matrix' if split == 'development' else 'reserve-matrix')
        try: result_obj=validate_paid_execution(run,expected_configuration=configuration(name),require_current_prompt=True)
        except (ValueError,TypeError,KeyError,OSError) as exc:
            raise ValueError(f'Saída da matriz tecnicamente incompleta: {exc}') from exc
        if run.get('configuration_id') != name or run.get('configuration') != configuration(name): raise ValueError('Candidato divergente')
        for field in ('source_id','configuration_id','audience','output_format','result_file','result_sha256'):
            if row.get(field) != run.get(field): raise ValueError('Metadados da matriz alterados')
        if digest(run['result_file']) != run.get('result_sha256'): raise ValueError('Saída da matriz alterada')
        result = result_obj.model_dump(mode='json')
        if run.get('source_sha256') != source_hashes[row['source_id']]: raise ValueError('Fonte da matriz alterada')
        from suno_asset.services.content_formats import validate_content, format_errors
        content = validate_content(result['content']) if result.get('content') else None
        issue_codes={issue.code for issue in result_obj.evaluation.issues} if result_obj.evaluation else set()
        infeasible = bool(
            result_obj.status.value=='needs_review' and result_obj.errors == () and content is not None
            and result_obj.evaluation is not None and not result_obj.evaluation.approved
            and result_obj.evaluation.deferred_due_to_format is True
            and issue_codes == {'format_noncompliant'} and format_errors(content)
            and all(not issue.hard_failure for issue in result_obj.evaluation.issues)
        )
        if row.get('verdict') != 'passed' or not row.get('reason'): raise ValueError('Matriz reprovada/inconclusiva')
        if infeasible:
            if (row.get('format_noncompliant_justified') is not True or not row.get('reason')
                or not row.get('divergence_resolution')):
                raise ValueError('Recusa inviável sem justificativa independente')
        else:
            if result_obj.status.value != 'approved' or not result.get('content'):
                raise ValueError('Saída entregue tecnicamente incompleta')
            if (content.audience.value != row['audience'] or content.output_format.value != row['output_format']
                or format_errors(content) or any(not c.evidence_refs for c in content.claims)):
                raise ValueError('Público/formato entregue divergente ou inviável')
            facts = row.get('facts') or []
            if (not facts or {f['id'] for f in facts} != inventory_ids[row['source_id']]
                or len({f['id'] for f in facts}) != len(facts) or any(
                f.get('state') != 'preserved' or not f.get('source_reference') or not f.get('support_excerpt') for f in facts)):
                raise ValueError('Inventário material da matriz incompleto')
        for metric in ('unsupported_claims','numeric_errors','attribution_errors','false_approvals','false_rejections'):
            if type(row.get(metric)) is not int or row[metric] < 0: raise ValueError('Métricas pendentes não equivalem a zero')
        if any(row[k] for k in ('unsupported_claims','numeric_errors','attribution_errors','false_approvals')):
            raise ValueError('Erros materiais/falsas aprovações na matriz')
        if (run.get('status') != 'approved' or row['false_rejections']) and not row.get('divergence_resolution'):
            raise ValueError('Divergência da matriz sem resolução')
    return form

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare-outputs','prepare-refinements','prepare-matrix','quality','freeze'))
    parser.add_argument('--corpus',type=Path,default=Path('tests/fixtures/evaluation/corpus.json'))
    parser.add_argument('--configuration',choices=CONFIGURATIONS)
    for arg in ('summary','form','account','outputs','cases','refinements','matrix','candidate-reviews','out'):parser.add_argument('--'+arg,type=Path,required=arg=='out')
    args=parser.parse_args()
    try:
        if args.action=='prepare-outputs':result=prepare_outputs(args.summary,args.corpus,automated=True)
        elif args.action=='prepare-refinements':result=prepare_refinement_review(args.summary,automated=True)
        elif args.action=='prepare-matrix':result=prepare_matrix_review(args.summary)
        elif args.action=='quality':result=validate_outputs(json.loads(args.outputs.read_text()),args.corpus)
        else:result=freeze_configuration(args.configuration,args.form,args.account,args.outputs,args.corpus,args.cases,args.refinements,args.matrix,args.candidate_reviews)
        _save(args.out,result)
    except (ValueError,TypeError,KeyError,AttributeError,OSError) as exc:parser.error(str(exc))
    print(json.dumps({'status':result['status'],'out':str(args.out)}))


if __name__=='__main__':main()
