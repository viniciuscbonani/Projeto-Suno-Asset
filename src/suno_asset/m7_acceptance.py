"""Prepara revisão de saídas, mede concordância e valida congelamento do M7.

Nenhum julgamento humano é preenchido automaticamente.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from suno_asset.m7_eval import _save
from suno_asset.m7_review import validate_review, VERDICTS, codex_metadata, validate_codex_identity, validate_inventories
from suno_asset.services.groq import PROMPT_VERSION, LITERAL_PROMPT_VERSION, MAX_CALLS, MAX_TOKENS, DEADLINE_SECONDS, SUPPORTED_MODELS
from suno_asset.services.glossary import GLOSSARY_VERSION
from suno_asset.workflow.graph import REAL_POLICY

ROOT = Path(__file__).resolve().parents[2]
CODE_FILES = (
    'src/suno_asset/services/sqlite_repository.py', 'src/suno_asset/services/artifact_lifecycle.py',
    'src/suno_asset/services/observability.py', 'src/suno_asset/services/pricing.py',
    'src/suno_asset/maintenance.py', 'src/suno_asset/services/source_preparation.py','src/suno_asset/services/local_storage.py',
    'src/suno_asset/services/protocols.py','src/suno_asset/workflow/state.py','src/suno_asset/services/literal.py',
    'src/suno_asset/m7_literal_eval.py','src/suno_asset/m7_literal_acceptance.py','src/suno_asset/m7_literal_refinement.py',
    'src/suno_asset/services/groq.py', 'src/suno_asset/services/quota_ledger.py', 'src/suno_asset/services/llm_budget.py',
    'src/suno_asset/services/parsing.py', 'src/suno_asset/services/retrieval.py',
    'src/suno_asset/services/glossary.py', 'src/suno_asset/workflow/graph.py',
    'src/suno_asset/api/app.py', 'src/suno_asset/domain/models.py', 'src/suno_asset/m7_eval.py',
    'src/suno_asset/m7_cases.py', 'src/suno_asset/m7_recovery.py', 'src/suno_asset/m7_budget.py', 'src/suno_asset/m7_acceptance.py', 'src/suno_asset/m7_review.py',
    'src/suno_asset/m7_promotion.py', 'src/suno_asset/services/content_formats.py',
    'src/suno_asset/services/provider_limits.py', 'src/suno_asset/config.py',
    'scripts/m7_wait_free_pilot.py', 'pyproject.toml', 'uv.lock')
FACT_STATES = {'preserved', 'omitted', 'altered', 'not_verified'}


def digest(path): return sha256(Path(path).read_bytes()).hexdigest()


def provider_calls_are_real(summary):
    """Separate real free inference from billing and from offline simulation.

    Legacy records used ``paid`` to mean provider calls, without proving a
    charge. Explicit modern fields take precedence; they never bypass the
    downstream audit, controls, fingerprint and release validations.
    """
    fields = {'provider_calls_real','quota_mode','authorized_billing_usd'}
    if fields & summary.keys():
        billing = summary.get('authorized_billing_usd')
        return (summary.get('paid') is False and summary.get('provider_calls_real') is True
            and summary.get('quota_mode') == 'free_only' and type(billing) in (int,float) and billing == 0)
    return summary.get('paid') is True


def validate_trace_hashes(run):
    from suno_asset.m7_recovery import validate_recovery_lineage
    validate_recovery_lineage(run)
    expected = run.get('audit_hashes') or {}
    strict = run.get('configuration_id') in {'current-quality','literal-quality','literal-hybrid'}
    audit_dir = Path(run['audit_dir']).resolve() if run.get('audit_dir') else None
    if strict and (audit_dir is None or not expected):
        raise ValueError('Execução paga sem trilha de auditoria obrigatória')
    if audit_dir:
        if not audit_dir.is_dir(): raise ValueError('Diretório de auditoria ausente')
        actual = {str(p.resolve()):digest(p) for p in audit_dir.rglob('*.json')}
        if actual != expected: raise ValueError('Auditoria de chamadas alterada/incompleta')
        if strict:
            import re
            checkpoints = {}
            for file in sorted(audit_dir.glob('attempt-*.json')):
                match = re.fullmatch(r'attempt-(\d+)-(started|received|interrupted)\.json',file.name)
                if not match: raise ValueError('Checkpoint de auditoria inválido')
                index,kind=int(match.group(1)),match.group(2)
                checkpoints.setdefault(index,{})[kind]=json.loads(file.read_text())
            if not checkpoints or set(checkpoints) != set(range(1,len(checkpoints)+1)):
                raise ValueError('Tentativas de auditoria ausentes ou fora de ordem')
            for index,parts in checkpoints.items():
                if set(parts) not in ({'started','received'},{'started','interrupted'}):
                    raise ValueError('Tentativa sem início/resposta única na auditoria')
                started=parts['started']; attempt=started.get('attempt') or {}; request=started.get('request') or {}
                stage=attempt.get('stage'); manifest=started.get('manifest') or {}
                models=manifest.get('models') or {}
                if (attempt.get('attempt') != index or not stage or attempt.get('model') != models.get(stage)
                    or request.get('model') != attempt.get('model')):
                    raise ValueError('Tentativa/modelo incompatível com manifesto')
                payload_hash=sha256(json.dumps(request,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                if attempt.get('payload_sha256') != payload_hash:
                    raise ValueError('Payload da tentativa diverge da auditoria')
                if 'received' in parts and not isinstance(parts['received'].get('response'),dict):
                    raise ValueError('Resposta recebida ausente da auditoria')
                if 'interrupted' in parts and parts['interrupted'].get('usage_known') is not False:
                    raise ValueError('Tentativa interrompida deve manter consumo desconhecido')
            if type(run.get('attempts')) is int and run['attempts'] != len(checkpoints):
                raise ValueError('Número de tentativas diverge da auditoria')
            if isinstance(run.get('attempts'),list):
                if len(run['attempts']) != len(checkpoints): raise ValueError('Tentativas do resumo divergem da auditoria')
                for attempt in run['attempts']:
                    index=attempt.get('attempt');parts=checkpoints.get(index,{})
                    terminal='interrupted' if attempt.get('status') in {'http_error','unavailable'} else 'received'
                    start=parts.get('started',{}).get('attempt',{})
                    if (not start or attempt.get('stage')!=start.get('stage') or attempt.get('model')!=start.get('model')
                        or attempt.get('payload_sha256')!=start.get('payload_sha256') or terminal not in parts):
                        raise ValueError('Tentativa registrada diverge do checkpoint terminal')
    for path, hashed in expected.items():
        if strict and (audit_dir is None or audit_dir not in Path(path).resolve().parents):
            raise ValueError('Arquivo de auditoria fora da raiz vinculada')
        if digest(path) != hashed: raise ValueError('Auditoria de chamadas alterada')
    return expected


def validate_execution_protocol(run, *, expected_protocol=None, purpose=None):
    """Bind a final trial to its immutable limits and resolved manifest."""
    import math
    protocol=run.get('execution_protocol')
    if (not isinstance(protocol,dict)
        or set(protocol)!={'version','purpose','max_refinements','stage_cost_limits'}
        or protocol.get('version')!='m7-execution-protocol-v1'
        or protocol.get('purpose') not in {'main','development-matrix','reserve-matrix'}
        or type(protocol.get('max_refinements')) is not int or protocol['max_refinements'] not in (0,1,2)):
        raise ValueError('Protocolo de execução ausente/inválido')
    caps=protocol.get('stage_cost_limits')
    if (not isinstance(caps,dict) or set(caps)!={'extract','verify','generate','evaluate','refine'}
        or any(type(v) not in (float,int) or not math.isfinite(v) or v<=0 for v in caps.values())):
        raise ValueError('Tetos do protocolo de execução inválidos')
    manifest=run.get('manifest') or {}
    if ((manifest.get('resolved_configuration') or {}).get('execution_protocol')!=protocol
        or manifest.get('stage_cost_limits_usd')!=caps
        or expected_protocol is not None and protocol!=expected_protocol
        or purpose is not None and protocol['purpose']!=purpose):
        raise ValueError('Protocolo diverge da rodada/manifesto/limites executados')
    return protocol


def validate_paid_execution(run, *, expected_configuration=None, require_current_prompt=False):
    """Rebuild technical completion from the result, budget, manifest and audit."""
    from suno_asset.domain.models import ExecutionMode, ExecutionResult, ExecutionStatus
    audit_hashes = validate_trace_hashes(run)
    if run.get('configuration_id') not in {'current-quality','literal-quality','literal-hybrid'}:
        raise ValueError('Identificador de configuração pago inválido')
    from suno_asset.m7_literal_eval import configuration
    expected = expected_configuration or configuration(run['configuration_id'])
    if run.get('configuration') != expected:
        raise ValueError('Configuração declarada diverge do candidato')
    manifest = run.get('manifest') or {}
    if require_current_prompt:
        expected_prompt = LITERAL_PROMPT_VERSION if expected['architecture'] == 'literal-v1' else PROMPT_VERSION
        if manifest.get('prompt_version') != expected_prompt:
            raise ValueError('Execução de qualificação exige versão atual do prompt')
    if (manifest.get('architecture') != expected['architecture'] or manifest.get('profile') != expected['profile']
        or manifest.get('models') != expected['models']):
        raise ValueError('Manifesto/modelos divergem da configuração candidata')
    result_path = Path(run.get('result_file') or '')
    if not result_path.is_file() or digest(result_path) != run.get('result_sha256'):
        raise ValueError('Resultado pago ausente ou alterado')
    result = ExecutionResult.model_validate_json(result_path.read_bytes())
    if result.manifest != manifest or result.architecture.value != expected['architecture'] or result.model_profile.value != expected['profile']:
        raise ValueError('Resultado diverge do manifesto/configuração')
    # Historical artifacts remain readable; current freeze/matrix contracts
    # require this binding explicitly before considering a candidate eligible.
    if 'execution_protocol' in run:
        validate_execution_protocol(run)
    resolved=manifest.get('resolved_configuration') or {}
    if (result.mode != ExecutionMode.REAL or resolved.get('architecture')!=expected['architecture']
        or resolved.get('model_profile')!=expected['profile']
        or resolved.get('audience')!=run.get('audience')
        or resolved.get('output_format')!=run.get('output_format')):
        raise ValueError('Modo ou configuração resolvida diverge do pedido executado')
    if (run.get('status') != result.status.value
        or run.get('errors') != [error.code for error in result.errors]
        or run.get('selected_revision') != result.selected_revision):
        raise ValueError('Status/erros/revisão declarados divergem do resultado persistido')
    budget_path = Path(run.get('budget_file') or '')
    if not budget_path.is_file() or digest(budget_path) != run.get('budget_sha256'):
        raise ValueError('Orçamento/trilha de tentativas ausente ou alterado')
    budget = json.loads(budget_path.read_text())
    if budget.get('manifest') != manifest or not isinstance(budget.get('attempts'),list):
        raise ValueError('Manifesto ou tentativas do orçamento divergem')
    audit_dir = Path(run['audit_dir']).resolve()
    starts = {}
    for artifact in audit_hashes:
        path = Path(artifact)
        if path.name.endswith('-started.json'):
            record = json.loads(path.read_text())
            starts[(record.get('attempt') or {}).get('attempt')] = record
    attempts = budget['attempts']
    if len(attempts) != len(starts) or not attempts:
        raise ValueError('Tentativas do orçamento não reconciliam com auditoria')
    stages=set()
    for index, attempt in enumerate(attempts,1):
        start=starts.get(index)
        if (start is None or attempt.get('attempt') != index
            or attempt.get('stage') != start['attempt'].get('stage')
            or attempt.get('model') != start['attempt'].get('model')
            or attempt.get('payload_sha256') != start['attempt'].get('payload_sha256')):
            raise ValueError('Tentativa incompleta ou divergente entre orçamento e auditoria')
        if attempt.get('model') != expected['models'].get(attempt.get('stage')):
            raise ValueError('Modelo de etapa divergente do perfil congelado')
    completed = validate_completed_attempts(run, attempts)
    validate_reported_usage_aggregates(run, budget, attempts)
    validate_source_materiality(result, budget, completed, audit_dir)
    for attempt in completed:
        stages.add(attempt['stage'])
    required={'extract','verify','generate','evaluate'}
    if result.evaluation is not None and result.evaluation.deferred_due_to_format:
        validate_structural_format_refusal(result, budget, completed, audit_dir)
        required = {'extract','verify','generate','refine'}
    if not required <= stages or (result.selected_revision and 'refine' not in stages):
        raise ValueError('Etapas de execução ausentes na trilha')
    if (result.status not in {ExecutionStatus.APPROVED,ExecutionStatus.NEEDS_REVIEW}
        or result.errors or result.content is None or result.evaluation is None
        or result.evaluation.revision != result.selected_revision
        or result.selected_revision not in {revision.number for revision in result.history}
        or not result.fact_verification or (expected['architecture']=='literal-v1'
            and result.fact_verification.processing_complete is not True)):
        raise ValueError('Resultado sem execução técnica completa e revisão selecionada')
    return result


def validate_reported_usage_aggregates(run, budget, attempts):
    """Recompute reported consumption and nominal cost from audited attempts.

    Unknown calls retain their full conservative reservation separately. They
    never contribute artificial reported tokens or nominal cost, and the
    selection gate still uses its conservative cost when usage is incomplete.
    """
    import math
    from suno_asset.m7_eval import PRICES
    from suno_asset.services.groq import _reported_usage
    expected = {}
    prompt_total = completion_total = 0
    for attempt in attempts:
        if attempt.get('usage_known') is not True:
            continue
        usage = _reported_usage(attempt)
        if usage is None or attempt.get('model') not in PRICES:
            raise ValueError('Agregados sem consumo conhecido/modelo validado')
        prompt, completion = usage
        prompt_total += prompt
        completion_total += completion
        row = expected.setdefault(attempt['stage'], {'calls':0,'prompt_tokens':0,
            'completion_tokens':0,'estimated_usd':0.0,'models':[]})
        row['calls'] += 1
        row['prompt_tokens'] += prompt
        row['completion_tokens'] += completion
        input_price, output_price = PRICES[attempt['model']]
        row['estimated_usd'] += (prompt * input_price + completion * output_price) / 1e6
        if attempt['model'] not in row['models']:
            row['models'].append(attempt['model'])
    if (type(run.get('attempts')) is not int or run['attempts'] != len(attempts)
        or type(run.get('usage_complete')) is not bool
        or run['usage_complete'] != all(a.get('usage_known') is True for a in attempts)):
        raise ValueError('Conclusão/número de consumo diverge das tentativas auditadas')
    for key, expected_total in (('prompt_tokens',prompt_total),('completion_tokens',completion_total)):
        if type(run.get(key)) is not int or run[key] != expected_total:
            raise ValueError('Total de consumo diverge das tentativas auditadas')

    def same_cost(value, actual):
        return (type(value) in (int,float) and math.isfinite(value)
            and math.isclose(value,actual,rel_tol=1e-9,abs_tol=1e-12))

    for container in (budget,run):
        stages = container.get('per_stage')
        if not isinstance(stages,dict) or set(stages) != set(expected):
            raise ValueError('Agregados por etapa divergem das tentativas auditadas')
        for stage, actual in expected.items():
            row = stages[stage]
            if (not isinstance(row,dict) or set(row) != set(actual)
                or any(type(row.get(key)) is not int or row[key] != actual[key]
                    for key in ('calls','prompt_tokens','completion_tokens'))
                or row.get('models') != actual['models']
                or not same_cost(row.get('estimated_usd'),actual['estimated_usd'])):
                raise ValueError('Consumo/modelo/custo por etapa diverge da auditoria')
    nominal_reported = round(sum(row['estimated_usd'] for row in expected.values()),6)
    if not same_cost(run.get('estimated_usd'),nominal_reported):
        raise ValueError('Custo nominal reportado diverge das tentativas auditadas')
    return {'prompt_tokens':prompt_total,'completion_tokens':completion_total,
        'per_stage':expected,'estimated_usd':nominal_reported,
        'usage_complete':all(a.get('usage_known') is True for a in attempts)}


def validate_source_materiality(result, budget, completed, audit_dir):
    """Reconcile the independent source pass before anchor-based comparison.

    Historical literal artifacts remain readable. New source-first executions
    must prove their complete source inventory from the received provider
    responses, rather than merely trusting a report's completion flag.
    """
    if result.architecture.value != 'literal-v1':
        return
    report = result.fact_verification
    events = budget.get('events') or []
    source_events = [e for e in events if e.get('stage') == 'source_materiality']
    version = result.manifest.get('prompt_version')
    enabled = (version in {'literal-v1.10', LITERAL_PROMPT_VERSION} or bool(source_events)
        or report is not None and (getattr(report, 'source_materiality_complete', None) is not None
            or getattr(report, 'source_materiality_checks', ())))
    if not enabled:
        return
    if (report is None or result.prepared_source is None or result.extraction is None
        or report.source_materiality_complete is not True or not source_events):
        raise ValueError('Controle source-first ausente ou incompleto')
    from suno_asset.services.literal import block_payload
    source = result.prepared_source
    blocks = {b.block_id:b for b in source.blocks}
    processing = {row.block_id:row for row in result.extraction.processing}
    anchors = {a.anchor_id:a for a in result.extraction.anchors}
    verified = {row.block_id:row for row in report.block_verifications}
    if (len(processing) != len(source.blocks) or set(processing) != set(blocks)
        or len(verified) != len(source.blocks) or set(verified) != set(blocks)):
        raise ValueError('Processamento/verificação source-first sem todos os blocos')
    rows = tuple(report.source_materiality_checks)
    if len(rows) != len(blocks) or {r.block_id for r in rows} != set(blocks):
        raise ValueError('Controle source-first sem todos os blocos únicos da fonte')
    for row in rows:
        if (row.classification == 'not_verified' or not row.reason.strip()
            or len(set(row.material_excerpt_ids)) != len(row.material_excerpt_ids)
            or set(row.material_excerpt_ids) - set(blocks[row.block_id].excerpt_ids)
            or (row.classification == 'material') != bool(row.material_excerpt_ids)):
            raise ValueError('Controle source-first com classificação ou suportes inválidos')
    audited_source, audited_comparison = [], []
    comparison_started = False
    editorial_started = False
    extraction_started = False
    for attempt in completed:
        if attempt['stage'] == 'extract':
            if version == LITERAL_PROMPT_VERSION:
                completed_source_ids=[row['block_id'] for event in audited_source
                    for row in event['output'].get('blocks',[])]
                if len(completed_source_ids)!=len(blocks) or set(completed_source_ids)!=set(blocks):
                    raise ValueError('Leitura cega source-first deve concluir todos os blocos antes da extração')
            extraction_started = True
        if attempt['stage'] in {'generate','refine'}:
            editorial_started = True
        if attempt['stage'] != 'verify':
            continue
        if editorial_started:
            raise ValueError('Controle source-first deve terminar antes da geração editorial')
        prefix = Path(audit_dir) / f'attempt-{attempt["attempt"]:03}'
        started = json.loads(prefix.with_name(prefix.name+'-started.json').read_text())['request']
        received = json.loads(prefix.with_name(prefix.name+'-received.json').read_text())['response']
        try:
            messages = [m for m in started['messages'] if m['role'] == 'user']
            if len(messages) != 1:
                raise ValueError('pedido sem usuário único')
            payload = json.loads(messages[0]['content'])
            choice = received['choices'][0]
            if choice.get('finish_reason') != 'stop':
                raise ValueError('resposta não encerrada')
            output = json.loads(choice['message']['content'])
            batch_ids = [b['block_id'] for b in payload['blocks']]
            if len(set(batch_ids)) != len(batch_ids) or not batch_ids or set(batch_ids)-set(blocks):
                raise ValueError('blocos do pedido inválidos')
            response_ids = [row['block_id'] for row in output['blocks']]
            if set(output) != {'blocks'} or len(response_ids) != len(batch_ids) or set(response_ids) != set(batch_ids):
                raise ValueError('resposta sem todos os blocos próprios do lote')
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError('Controle source-first sem pedido/resposta integral auditado') from exc
        expected_source = block_payload(source, tuple(blocks[bid] for bid in batch_ids))
        if version in {'literal-v1.10', LITERAL_PROMPT_VERSION} and ((started.get('response_format') or {}).get('json_schema') or {}).get('name') != 'suno_verify_' + version.replace('.', '_'):
            raise ValueError('Schema do controle source-first diverge da versão candidata')
        if payload.get('control') == 'source_materiality':
            if version == LITERAL_PROMPT_VERSION and extraction_started:
                raise ValueError('Leitura cega source-first não pode começar ou continuar depois da extração')
            if comparison_started or payload != {'control':'source_materiality', **expected_source}:
                raise ValueError('Controle source-first deve preceder comparação sem âncoras/rótulos')
            audited_source.append({'stage':'source_materiality','block_ids':batch_ids,'output':output})
        else:
            comparison_started = True
            expected_comparison = {**expected_source,
                'processing':[processing[bid].model_dump(mode='json') for bid in batch_ids],
                'anchors':[{'id':a.anchor_id,'kind':a.kind.value,'importance':a.importance.value,
                    'owner_block_id':a.owner_block_id,'excerpt_ids':[e.excerpt_id for e in a.support_excerpts],
                    'numeric_facts':[f.model_dump(mode='json') for f in a.numeric_facts]}
                    for bid in batch_ids for aid in processing[bid].anchor_ids for a in (anchors[aid],)]}
            if not audited_source or payload != expected_comparison:
                raise ValueError('Comparação sem fonte exata ou controle source-first anterior')
            audited_comparison.append({'stage':'block_verification','output':output})
    if not audited_comparison or source_events != audited_source:
        raise ValueError('Controle source-first diverge dos eventos/respostas auditados')
    comparison_events = [e for e in events if e.get('stage') == 'block_verification']
    if comparison_events != audited_comparison:
        raise ValueError('Comparação de blocos diverge da resposta auditada')
    first_comparison = next((i for i,e in enumerate(events) if e.get('stage') == 'block_verification'), None)
    if first_comparison is None or any(i > first_comparison for i,e in enumerate(events)
            if e.get('stage') == 'source_materiality'):
        raise ValueError('Eventos source-first posteriores à comparação')
    audited_rows = [row for event in audited_source for row in event['output'].get('blocks', [])]
    if audited_rows != [row.model_dump(mode='json') for row in rows]:
        raise ValueError('Parecer source-first diverge dos suportes recebidos do provedor')
    comparison_rows = [row for event in audited_comparison for row in event['output'].get('blocks', [])]
    if (len(comparison_rows) != len(blocks)
        or {row.get('block_id') for row in comparison_rows} != set(blocks)):
        raise ValueError('Comparação source-first sem controle de todos os blocos')
    from suno_asset.domain.models import BlockVerification
    for raw in comparison_rows:
        original = BlockVerification.model_validate(raw)
        selected = verified[original.block_id]
        independent = next(row for row in rows if row.block_id == original.block_id)
        candidate = processing[original.block_id].classification
        expected_coverage, expected_relevance = original.coverage.value, original.relevance.value
        expected_omissions = original.omission_excerpt_ids
        anchor_ids = [a.anchor_id for a in original.anchors]
        if (len(anchor_ids) != len(processing[original.block_id].anchor_ids)
            or set(anchor_ids) != set(processing[original.block_id].anchor_ids)
            or len(set(expected_omissions)) != len(expected_omissions)
            or set(expected_omissions)-set(blocks[original.block_id].excerpt_ids)
            or original.coverage.value == 'passed' and expected_omissions
            or original.coverage.value == 'failed' and not expected_omissions):
            raise ValueError('Comparação source-first com âncoras/omissões inválidas')
        if independent.classification == 'material' and candidate != 'material':
            expected_coverage = expected_relevance = 'failed'
            expected_omissions = tuple(dict.fromkeys((*expected_omissions,*independent.material_excerpt_ids)))
        elif independent.classification in {'administrative','no_relevant_facts'} and candidate == 'material':
            expected_relevance = 'failed'
        if (selected.support != original.support or selected.anchors != original.anchors
            or selected.coverage.value != expected_coverage or selected.relevance.value != expected_relevance
            or selected.omission_excerpt_ids != expected_omissions):
            raise ValueError('Parecer de blocos source-first diverge da comparação auditada')
    for row in rows:
        if row.classification == 'material' and processing[row.block_id].classification != 'material':
            check = verified[row.block_id]
            if (check.coverage.value != 'failed' or check.relevance.value != 'failed'
                or report.approved or result.status.value == 'approved'):
                raise ValueError('Fonte material omitida não pode obter aprovação')


def validate_structural_format_refusal(result, budget, completed, audit_dir):
    """Prove a local structural refusal after two real editorial corrections.

    This never certifies semantics or approval. The independent matrix review
    still has to justify editorial infeasibility; budget/provider/factual
    failures and unattempted refinements cannot qualify for this exception.
    """
    from types import SimpleNamespace
    from suno_asset.services.content_formats import format_errors
    from suno_asset.services.literal import LiteralEvaluator
    from suno_asset.workflow.graph import LITERAL_POLICY
    if (result.architecture.value != 'literal-v1' or result.status.value != 'needs_review'
        or result.errors or result.selected_revision != 2 or result.evaluation.approved
        or result.prepared_source is None or result.extraction is None
        or result.fact_verification is None or not result.fact_verification.approved
        or result.fact_verification.processing_complete is not True
        or result.fact_verification.issues or result.evaluation.policy != LITERAL_POLICY):
        raise ValueError('Recusa estrutural sem fonte verificada e duas correções editoriais completas')
    anchors = result.extraction.anchors
    if ({a.anchor_id for a in anchors} != set(result.fact_verification.anchor_ids)
        or {b.block_id for b in result.prepared_source.blocks} !=
            {b.block_id for b in result.fact_verification.block_verifications}):
        raise ValueError('Recusa estrutural sem controle integral da fonte')
    history = sorted(result.history, key=lambda revision: revision.number)
    if len(history) != 3 or [r.number for r in history] != [0,1,2]:
        raise ValueError('Recusa estrutural exige original e duas revisões editoriais avaliadas')
    events = budget.get('events')
    if not isinstance(events,list):
        raise ValueError('Prechecks locais e correções ausentes da auditoria de orçamento')
    prechecks = [event for event in events if event.get('stage') == 'format_precheck']
    if len(prechecks) != 3 or [event.get('revision') for event in prechecks] != [0,1,2]:
        raise ValueError('Prechecks de todas as revisões ausentes')
    for revision, event in zip(history,prechecks):
        problems = format_errors(revision.content) if revision.content is not None else ()
        if (revision.error is not None or revision.content is None or revision.evaluation is None
            or not problems or {issue.code for issue in problems} != {'format_noncompliant'}):
            raise ValueError('Recusa estrutural contém erro ou revisão sem apresentação inválida comprovada')
        # This call takes only the deterministic invalid-format branch. It
        # cannot reach provider transport, since problems were checked above.
        expected_report = LiteralEvaluator(SimpleNamespace(literal_artifacts=[])).evaluate(
            revision.content, anchors, (), LITERAL_POLICY)
        if (revision.evaluation != expected_report
            or event.get('output') != expected_report.model_dump(mode='json')):
            raise ValueError('Precheck estrutural diverge da avaliação integral registrada')
    editorial_events = [event for event in events if event.get('stage') in {'generate','refine'}]
    editorial_calls = [attempt for attempt in completed if attempt.get('stage') in {'generate','refine'}]
    if (len(editorial_events) != len(editorial_calls)
        or {event.get('revision') for event in editorial_events if event.get('stage') == 'refine'} != {1,2}):
        raise ValueError('Duas correções reais não reconciliam com tentativas auditadas')
    for event, attempt in zip(editorial_events,editorial_calls):
        number = event.get('revision')
        if event.get('stage') != attempt['stage'] or number not in ({0} if attempt['stage']=='generate' else {1,2}):
            raise ValueError('Etapa/revisão editorial divergente na auditoria')
        prefix = audit_dir / f'attempt-{attempt["attempt"]:03}'
        received = json.loads(prefix.with_name(prefix.name+'-received.json').read_text())['response']
        try:
            output = json.loads(received['choices'][0]['message']['content'])
        except (KeyError,IndexError,TypeError,ValueError) as exc:
            raise ValueError('Correção editorial sem resposta completa auditada') from exc
        if event.get('output') != output:
            raise ValueError('Saída da correção diverge da resposta do provedor')
        if attempt['stage'] == 'refine':
            started = json.loads(prefix.with_name(prefix.name+'-started.json').read_text())['request']
            try:
                payload = json.loads(next(message['content'] for message in started['messages'] if message['role']=='user'))
                previous_claims = payload['previous']['claims']
            except (KeyError,StopIteration,TypeError,ValueError) as exc:
                raise ValueError('Correção sem afirmações anteriores exatas na auditoria') from exc
            originals = {claim.claim_id:claim for claim in history[number-1].content.claims}
            if not previous_claims or any(
                claim.get('claim_id') not in originals
                or claim.get('text') != originals[claim['claim_id']].text
                or claim.get('anchor_ids') != list(originals[claim['claim_id']].anchor_ids)
                for claim in previous_claims):
                raise ValueError('Correção não preserva as afirmações anteriores auditadas')
    return result


def validate_completed_attempts(run, attempts=None):
    """Accept completed controls after only the transport's bounded retries.

    A recovered response error does not erase its unknown consumption. Audit
    evidence, identical retry requests and a successful terminal response are
    mandatory; network failures and unfinished controls remain blocking.
    """
    validate_trace_hashes(run)
    from suno_asset.services.groq import _reported_usage
    attempts = attempts if attempts is not None else run.get('attempts')
    if not isinstance(attempts, list) or not attempts:
        raise ValueError('Trilha de tentativas completas ausente')
    audit_dir = Path(run['audit_dir'])
    completed = []
    pending = None
    retry_kinds = set()
    for index, attempt in enumerate(attempts, 1):
        identity = tuple(attempt.get(key) for key in ('stage','model','payload_sha256'))
        if (attempt.get('attempt') != index or not all(identity)
            or pending is not None and identity != pending):
            raise ValueError('Retry recuperado diverge do pedido/etapa/modelo original')
        start_path = audit_dir / f'attempt-{index:03}-started.json'
        start = json.loads(start_path.read_text())['attempt']
        if any(attempt.get(key) != start.get(key) for key in ('attempt','stage','model','payload_sha256')):
            raise ValueError('Tentativa diverge do início auditado')
        if attempt.get('status') == 'complete':
            received_path = audit_dir / f'attempt-{index:03}-received.json'
            if not received_path.is_file():
                raise ValueError('Controle completo sem resposta auditada')
            response = json.loads(received_path.read_text()).get('response')
            usage = response.get('usage') if isinstance(response,dict) else None
            reported = _reported_usage(usage)
            known = attempt.get('usage_known')
            if type(known) is not bool or known != (reported is not None):
                raise ValueError('Consumo conhecido diverge da resposta auditada')
            if (type(attempt.get('input_estimate')) is not int or attempt['input_estimate'] <= 0
                or type(attempt.get('output_reserved')) is not int or attempt['output_reserved'] <= 0
                or any(attempt.get(key) != start.get(key) for key in ('input_estimate','output_reserved'))):
                raise ValueError('Consumo sem reserva integral original auditada')
            reservation = attempt['input_estimate'] + attempt['output_reserved']
            if 'total_reserved' in attempt and (type(attempt['total_reserved']) is not int
                or attempt['total_reserved'] != reservation):
                raise ValueError('Reserva total diverge da reserva original auditada')
            if known:
                recorded = _reported_usage(attempt)
                if recorded != reported or ('usage_invalid' in attempt
                    and attempt['usage_invalid'] is not None and attempt['usage_invalid'] is not False):
                    raise ValueError('Contadores conhecidos divergem do consumo auditado')
                actual = sum(reported)
                if reported[0] > attempt['input_estimate'] or reported[1] > attempt['output_reserved']:
                    raise ValueError('Controle completo com consumo acima da estimativa auditada')
                if 'estimate_underflow' in attempt and attempt['estimate_underflow'] is not False:
                    raise ValueError('Controle completo com estimativa inconsistente')
                for key, expected in (('settled_tokens',actual),
                    ('released_tokens',max(0,reservation-actual))):
                    if key in attempt and (type(attempt[key]) is not int or attempt[key] != expected):
                        raise ValueError('Liquidação de consumo diverge da resposta auditada')
            else:
                # Legacy unknown records may have null/missing counters or
                # zero derived metrics. They are never reported usage and
                # cannot release any part of the original reservation.
                if any(attempt.get(key) is not None and
                    (type(attempt[key]) is not int or attempt[key] != 0)
                    for key in ('prompt_tokens','completion_tokens')):
                    raise ValueError('Consumo desconhecido contém contadores fabricados')
                if attempt.get('settled_tokens') is not None or ('released_tokens' in attempt and
                    (type(attempt['released_tokens']) is not int or attempt['released_tokens'] != 0)):
                    raise ValueError('Consumo desconhecido não pode liberar reserva')
                if 'usage_invalid' in attempt and attempt['usage_invalid'] is not True:
                    raise ValueError('Indicador de consumo inválido diverge da resposta auditada')
                if ('total_tokens' in attempt
                    and not (type(attempt['total_tokens']) is int and attempt['total_tokens'] == 0)
                    and (not isinstance(usage,dict) or 'total_tokens' not in usage or
                        json.dumps(attempt['total_tokens']) != json.dumps(usage['total_tokens']))):
                    raise ValueError('Total desconhecido diverge da resposta auditada')
            completed.append(attempt)
            pending = None
            retry_kinds.clear()
            continue
        if attempt.get('status') != 'http_error':
            raise ValueError('Tentativa não recuperável ou controle incompleto')
        interrupted = json.loads((audit_dir / f'attempt-{index:03}-interrupted.json').read_text())
        status = attempt.get('http_status')
        code = attempt.get('provider_code')
        try:
            provider_error = json.loads(interrupted.get('provider_response',''))['error']
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError('Retry sem erro explícito auditado do provedor') from exc
        kind = (status, code)
        if (kind not in {(400,'json_validate_failed'),(429,'rate_limit_exceeded')}
            or interrupted.get('http_status') != status or interrupted.get('usage_known') is not False
            or not isinstance(provider_error,dict) or provider_error.get('code') != code
            or attempt.get('usage_known') is not False or kind in retry_kinds
            or type(attempt.get('input_estimate')) is not int or attempt['input_estimate'] <= 0
            or type(attempt.get('output_reserved')) is not int or attempt['output_reserved'] <= 0
            or any(attempt.get(key) != start.get(key) for key in ('input_estimate','output_reserved'))
            or any(attempt.get(key) is not None and
                (type(attempt[key]) is not int or attempt[key] != 0)
                for key in ('prompt_tokens','completion_tokens','total_tokens'))
            or attempt.get('settled_tokens') is not None
            or 'released_tokens' in attempt and
                (type(attempt['released_tokens']) is not int or attempt['released_tokens'] != 0)):
            raise ValueError('Retry não permitido, sem reserva integral ou fora do limite')
        retry_kinds.add(kind)
        pending = identity
    if pending is not None:
        raise ValueError('Retry sem controle concluído posterior')
    if run.get('usage_complete') is True and any(a.get('usage_known') is not True for a in attempts):
        raise ValueError('Uso desconhecido não pode ser declarado completo')
    return completed


def technical_attempts_complete(run, attempts=None):
    """Conservative predicate for recording a completed persistent quota job."""
    try:
        validate_completed_attempts(run, attempts)
    except (ValueError,TypeError,KeyError,OSError):
        return False
    return True


def audit_replay_session(run, expected_configuration):
    """Replay completed provider controls locally, matching their exact requests.

    Transport retries stay in the validated audit and consumption history; only
    their successfully completed controls are replayed. No provider or quota
    transport is reachable from this session.
    """
    from suno_asset.services.groq import GroqSession
    manifest = run.get('manifest') or {}
    if any(manifest.get(key) != expected_configuration[key]
           for key in ('architecture', 'profile', 'models')):
        raise ValueError('Manifesto do controle diverge da configuração candidata')
    completed = validate_completed_attempts(run)
    audit_dir = Path(run['audit_dir'])

    class ReplaySession(GroqSession):
        def preflight(self, *args, **kwargs):
            return None

        def call(self, stage, system, data, schema, output_cap=None):
            if self.replay_index >= len(completed):
                raise ValueError('Controle completo sem resposta auditada correspondente')
            attempt = completed[self.replay_index]
            prefix = audit_dir / f'attempt-{attempt["attempt"]:03}'
            start = json.loads(prefix.with_name(prefix.name+'-started.json').read_text())
            expected_payload = self.payload(stage, system, data, schema, output_cap)
            if (attempt['stage'] != stage or start.get('manifest') != manifest
                or start.get('request') != expected_payload):
                raise ValueError('Pedido/schema/modelo do controle diverge da reconstrução auditada')
            response = json.loads(prefix.with_name(prefix.name+'-received.json').read_text()).get('response')
            try:
                choice = response['choices'][0]
                if choice.get('finish_reason') != 'stop' or choice['message'].get('refusal'):
                    raise ValueError('resposta incompleta ou recusada')
                output = json.loads(choice['message']['content'])
                if not isinstance(output, dict):
                    raise ValueError('resposta estruturada não é objeto')
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError('Controle sem resposta JSON final válida auditada') from exc
            self.replay_index += 1
            return output

        def assert_exhausted(self):
            if self.replay_index != len(completed):
                raise ValueError('Respostas auditadas não reconciliam com todos os controles executados')

    session = ReplaySession('offline-audit-replay', transport=lambda *_: None,
        tpm_limit=manifest.get('tpm_limit', 8000), context_limit=manifest.get('context_limit', 131072))
    session.configure(expected_configuration['architecture'], expected_configuration['profile'])
    session.model = expected_configuration['models']['evaluate']
    if manifest.get('prompt_version') != session.prompt_version:
        raise ValueError('Versão de prompt do controle diverge da versão candidata')
    session.replay_index = 0
    return session


def validate_case_execution(run, case, pages, expected_configuration):
    """Recompute a case prediction from its exact source-context and responses."""
    from suno_asset.m7_cases import evaluate_case
    from suno_asset.services.protocols import ExpectedServiceError
    if (run.get('configuration') != expected_configuration
        or run.get('model') != expected_configuration['models']['evaluate']
        or run.get('source_id') != case['source_id'] or run.get('id') != case['id']
        or run.get('error') or not run.get('prediction')):
        raise ValueError('Caso sem configuração/resultado completo correspondente')
    session = audit_replay_session(run, expected_configuration)
    try:
        result = evaluate_case(case, pages, session)
    except ExpectedServiceError as exc:
        raise ValueError('Resposta do caso fora do contrato semântico auditado') from exc
    session.assert_exhausted()
    if result.model_dump(mode='json') != run['prediction']:
        raise ValueError('Predição do caso diverge da resposta auditada e normalização de evidência')
    return result


def identified(form, time_field):
    if form.get('review_type') == 'codex':
        validate_codex_identity(form, time_field)
        return
    if form.get('status') != 'reviewed' or not isinstance(form.get('reviewer'), str) or not form['reviewer'].strip():
        raise ValueError('Revisão humana pendente: responsável e status reviewed obrigatórios')
    timestamp = datetime.fromisoformat(form.get(time_field) or '')
    if timestamp.utcoffset() is None: raise ValueError('Data precisa conter fuso')


def validate_account(form):
    if form.get('version') == 'm7-public-account-v3':
        if form.get('status') != 'recorded' or form.get('scope') != 'public_copom_only':
            raise ValueError('Registro da conta exige escopo somente atas públicas')
        if form.get('retention') not in {'unknown', 'documented'} or form.get('private_use_authorized') is not False:
            raise ValueError('Retenção desconhecida não autoriza uso privado')
        if form.get('retention') == 'documented' and not form.get('retention_evidence'):
            raise ValueError('Retenção sem evidência deve permanecer desconhecida')
        limits = form.get('limits') or {}
        if type(limits.get('tokens_per_minute')) is not int or limits['tokens_per_minute'] < 1000:
            raise ValueError('Limite TPM efetivo inválido')
        for key in ('requests_per_minute', 'tokens_per_day', 'requests_per_day'):
            if limits.get(key) is not None and (type(limits[key]) is not int or limits[key] <= 0):
                raise ValueError('Limites diários e por minuto devem ser separados')
        if limits['tokens_per_minute'] > 8000 and not form.get('limits_evidence'):
            raise ValueError('Não substituir TPM observado por hipóteses sem evidência')
        if form.get('unverified_hypotheses') != {'requests': 900, 'tokens': 240000, 'model': None, 'window': None}:
            raise ValueError('Hipóteses devem registrar modelo/janela desconhecidos')
        return form
    identified(form, 'verified_at')
    required_text = ('scope_description', 'effective_retention_description', 'settings_evidence_description', 'rationale')
    if any(not isinstance(form.get(key), str) or not form[key].strip() for key in required_text):
        raise ValueError('Registro de retenção/escopo/evidência incompleto')
    if type(form.get('zdr_enabled')) is not bool or type(form.get('split_input_output_limits')) is not bool:
        raise ValueError('Conferir ZDR e limites separados na conta')
    if type(form.get('tpm_limit')) is not int or form['tpm_limit'] < 1000:
        raise ValueError('Limite TPM efetivo inválido')
    if form.get('accepted_for_public_copom_use') is not True:
        raise ValueError('Configuração efetiva ainda não aceita para o uso proposto')
    return form


def prepare_outputs(summary_path: Path, corpus_path: Path, *, automated=False):
    summary = json.loads(summary_path.read_text())
    corpus = json.loads(corpus_path.read_text())
    sources = {s['id']: s for s in corpus['sources']}
    rows = []
    for run in summary['rows']:
        audit_hashes = validate_trace_hashes(run)
        source = sources[run['source_id']]
        if run['source_sha256'] != source['sha256']: raise ValueError('Hash da fonte da rodada diverge do corpus')
        result_path = Path(run['result_file'])
        if 'result_sha256' in run and digest(result_path) != run['result_sha256']:
            raise ValueError('Resultado alterado desde a execução da rodada')
        result = json.loads(result_path.read_text())
        anchor_path = result_path.with_name(result_path.stem + '-anchors.json')
        for revision in result['history']:
            if not revision.get('content'): continue
            number = revision['number']
            rows.append({'id': f'{digest(result_path)}:revision-{number}', 'source_id': source['id'],
                'source_sha256': source['sha256'], 'split': source['split'], 'model': run['model'],
                'repetition': run.get('repetition', 1), 'revision': number,
                **({'configuration_id':run['configuration_id'],'configuration':run['configuration'],
                    'manifest':run['manifest'],'independent_metrics':{'unsupported_claims':None,'numeric_errors':None,'attribution_errors':None},
                    'trace_files':{**{str(p.resolve()):digest(p) for p in result_path.parent.glob(result_path.stem+'-budget.json')},**audit_hashes}}
                    if 'configuration_id' in run else {}),
                'selected': result['selected_revision'] == number,
                'result_file': str(result_path.resolve()), 'result_sha256': digest(result_path),
                'anchors_file': str(anchor_path.resolve()), 'anchors_sha256': digest(anchor_path) if anchor_path.exists() else None,
                'automatic_approved': bool(revision.get('evaluation') and revision['evaluation']['approved']),
                'technical_error': revision.get('error', {}).get('code') if revision.get('error') else None,
                'criteria': {name: {'verdict': None, 'reason': None} for name in REAL_POLICY.required_checks},
                'critical_fact_inventory_reviewed': False,
                'critical_facts': [{'id': f'C{i:02}', 'source_reference': None,
                    'anchors': None, 'text': None, 'reason': None} for i in range(1,13)] if source['id'].startswith('copom281-') else [],
                'instruction': 'Conferir a fonte integral; completar a lista de fatos críticos aplicáveis, não limitar a revisão à busca de palavras.'})
    if automated:
        for row in rows:
            row['independent_metrics'] = {key: None for key in ('unsupported_claims', 'numeric_errors', 'attribution_errors', 'false_approvals', 'false_rejections')}
            row['divergence_resolution'] = None
            row['format_verdict'] = None
            row['format_reason'] = None
            for fact in row['critical_facts']:
                fact['support_excerpt'] = None
        return {'version': 'm7-codex-output-review-v3', 'status': 'pending', **codex_metadata(),
            'inventories':{},
            'reviewed_at': None, 'summary_file': str(summary_path.resolve()),
            'summary_sha256': digest(summary_path), 'rows': rows}
    return {'version': 'm7-output-review-v1', 'status': 'pending', 'reviewer': None, 'reviewed_at': None,
        'summary_file': str(summary_path.resolve()), 'summary_sha256': digest(summary_path),
        'rows': rows, 'limitation': 'Revisão do texto completo e das âncoras, independente dos pareceres automáticos.'}


def validate_outputs(form: dict, corpus_path: Path):
    identified(form, 'reviewed_at')
    summary_path = Path(form['summary_file'])
    if digest(summary_path) != form['summary_sha256']: raise ValueError('Rodada alterada desde a preparação')
    automated = form.get('version') == 'm7-codex-output-review-v3'
    if automated and not provider_calls_are_real(json.loads(summary_path.read_text())):
        raise ValueError('Revisão v3 exige execuções reais completas')
    expected = prepare_outputs(summary_path, corpus_path, automated=automated)
    by_id = {r['id']: r for r in expected['rows']}
    rows = form.get('rows') or []
    if not rows or len(rows) != len(by_id) or {r['id'] for r in rows} != set(by_id):
        raise ValueError('Saídas ausentes, duplicadas ou desconhecidas')
    inventory_ids = validate_inventories(form, {r['source_id']:r['source_sha256'] for r in expected['rows']}) if automated else {}
    false_approvals = false_rejections = critical_false_approvals = 0
    metrics = {}
    decisions = []
    for row in rows:
        original = by_id[row['id']]
        for key in ('source_id','source_sha256','split','model','repetition','revision','selected','result_file',
                    'result_sha256','anchors_file','anchors_sha256','automatic_approved','technical_error'):
            if row.get(key) != original[key]: raise ValueError('Metadados/artefatos da saída alterados')
        if 'configuration_id' in original:
            for key in ('configuration_id','configuration','manifest','trace_files'):
                if row.get(key)!=original[key]:raise ValueError('Configuração/rastreabilidade alterada')
            measures=row.get('independent_metrics') or {}
            measure_keys = {'unsupported_claims','numeric_errors','attribution_errors'} | ({'false_approvals','false_rejections'} if automated else set())
            if set(measures)!=measure_keys or any(type(v) is not int or v<0 for v in measures.values()):
                raise ValueError('Métricas independentes pendentes por revisão')
        criteria = row.get('criteria') or {}
        if set(criteria) != set(REAL_POLICY.required_checks) or any(
            v.get('verdict') not in VERDICTS or not isinstance(v.get('reason'), str) or not v['reason'].strip()
            for v in criteria.values()): raise ValueError('Parecer humano incompleto por critério')
        facts = row.get('critical_facts') or []
        if row.get('critical_fact_inventory_reviewed') is not True or not facts or len({f['id'] for f in facts}) != len(facts):
            raise ValueError('Inventário de fatos críticos pendente ou duplicado')
        if not automated and original['source_id'].startswith('copom281-') and not {f'C{i:02}' for i in range(1,13)} <= {f['id'] for f in facts}:
            raise ValueError('Cobertura C01–C12 incompleta')
        if automated and {f['id'] for f in facts} != inventory_ids[original['source_id']]:
            raise ValueError('Cobertura de todos os fatos materiais do inventário pendente')
        if any(f.get('anchors') not in FACT_STATES or f.get('text') not in FACT_STATES
               or not f.get('reason') or not f.get('source_reference') for f in facts):
            raise ValueError('Conferência semântica por fato incompleta')
        if automated and (any(not f.get('support_excerpt') for f in facts)
            or row.get('format_verdict') not in VERDICTS or not row.get('format_reason')):
            raise ValueError('Suporte exato/formato na revisão Codex pendente')
        critical_error = any(f['text'] != 'preserved' or f['anchors'] != 'preserved' for f in facts)
        human_passed = all(c['verdict'] == 'passed' for c in criteria.values()) and not critical_error
        if 'configuration_id' in original:human_passed=human_passed and not any(row['independent_metrics'][k] for k in ('unsupported_claims','numeric_errors','attribution_errors'))
        if automated:
            human_passed = human_passed and row['format_verdict'] == 'passed'
            if (bool(row['automatic_approved']) != human_passed or row['independent_metrics']['false_approvals']
                or row['independent_metrics']['false_rejections']) and not row.get('divergence_resolution'):
                raise ValueError('Divergência automática sem resolução')
        automatic = row['automatic_approved']
        false_approvals += bool(automatic and not human_passed)
        critical_false_approvals += bool(automatic and (critical_error or any(
            criteria[k]['verdict'] != 'passed' for k in ('coverage','support','numbers','citations'))))
        false_rejections += bool(not automatic and human_passed and not row['technical_error'])
        model_metrics = metrics.setdefault(row['model'], {'reviewed_revisions': 0, 'human_passed': 0,
            'critical_facts_preserved': 0, 'critical_facts_total': 0})
        model_metrics['reviewed_revisions'] += 1
        model_metrics['human_passed'] += human_passed
        model_metrics['critical_facts_preserved'] += sum(f['text'] == 'preserved' for f in facts)
        model_metrics['critical_facts_total'] += len(facts)
        decisions.append({**row, 'human_passed': human_passed, 'critical_error': critical_error})
    summary = json.loads(summary_path.read_text())
    regressions = []
    grouped = {}
    for row in decisions:
        grouped.setdefault(row['result_sha256'], []).append(row)
    for revisions in grouped.values():
        revisions.sort(key=lambda row: row['revision'])
        for before, after in zip(revisions, revisions[1:]):
            old = {f['id']: f for f in before['critical_facts']}
            new = {f['id']: f for f in after['critical_facts']}
            if (any(f['text'] == 'preserved' and (identifier not in new or new[identifier]['text'] != 'preserved')
                   for identifier, f in old.items()) or any(c['verdict']=='passed' and
                    after['criteria'][name]['verdict']!='passed' for name,c in before['criteria'].items())):
                regressions.append(after['id'])
    return {'status': 'reviewed', 'reviewer': form.get('reviewer'), 'reviewed_at': form['reviewed_at'],
        **({key: form[key] for key in ('review_type','session_id','model','instructions_version','limitations','passes','unresolved_divergences')} if automated else {}),
        'reviewed_revisions': len(rows), 'false_approvals': false_approvals,
        'critical_false_approvals': critical_false_approvals, 'false_rejections': false_rejections,
        **({'review_reported_false_approvals':sum(r['independent_metrics']['false_approvals'] for r in decisions),
            'review_reported_false_rejections':sum(r['independent_metrics']['false_rejections'] for r in decisions)} if automated else {}),
        'refinement_regressions': regressions, 'models': metrics, 'configurations': {identifier:{'selected_runs':sum(r.get('configuration_id')==identifier and r['selected'] for r in decisions),
            'human_passed':sum(r.get('configuration_id')==identifier and r['human_passed'] and r['selected'] for r in decisions)}
            for identifier in {r.get('configuration_id') for r in decisions if r.get('configuration_id')}},
        'run_fingerprint': summary.get('fingerprint'),
        'pipeline_runs': len(summary['rows']), 'run_rows': summary['rows'],
        'technical_failures': sum(bool(r['errors']) for r in summary['rows']),
        'decisions': decisions, 'limitation': 'Amostra pequena; métricas não demonstram acurácia geral.'}


def fingerprint(corpus_path: Path):
    corpus = json.loads(corpus_path.read_text())
    from suno_asset.m7_literal_eval import CONFIGURATIONS,configuration
    from suno_asset.services.source_preparation import SEGMENTATION_VERSION,TARGET_SOURCE_TOKENS,MAX_SOURCE_TOKENS
    from suno_asset.workflow.graph import LITERAL_POLICY
    return {'literal_prompt_version':LITERAL_PROMPT_VERSION,'literal_policy_version':LITERAL_POLICY.version,
        'segmentation_version':SEGMENTATION_VERSION,'source_target_tokens':TARGET_SOURCE_TOKENS,'source_max_tokens':MAX_SOURCE_TOKENS,
        'configurations':[configuration(name) for name in CONFIGURATIONS], 'prompt_version': PROMPT_VERSION, 'policy_version': REAL_POLICY.version,
        'required_checks': list(REAL_POLICY.required_checks), 'glossary_version': GLOSSARY_VERSION,
        'max_calls': MAX_CALLS, 'max_tokens': MAX_TOKENS, 'deadline_seconds': DEADLINE_SECONDS,
        'code_hashes': {file: digest(ROOT / file) for file in CODE_FILES},
        'source_hashes': {s['id']: s['sha256'] for s in corpus['sources']},
        'case_hashes': {s['case_file']: digest(corpus_path.parent / s['case_file']) for s in corpus['sources'] if s['split']=='development'}}


def validate_frozen(path: Path, corpus_path: Path, models: list[str]):
    frozen = json.loads(path.read_text())
    if frozen.get('status') != 'frozen' or frozen.get('fingerprint') != fingerprint(corpus_path):
        raise ValueError('Configuração não congelada ou código/corpus mudou')
    if models != [frozen.get('model')]: raise ValueError('Reserva exige apenas o modelo escolhido e congelado')
    for file, expected_hash in frozen.get('review_artifacts', {}).items():
        if digest(file) != expected_hash: raise ValueError('Revisão/conta alterada após congelamento')
    if len(frozen.get('review_artifacts', {})) != 4: raise ValueError('Artefatos de revisão/conferência ausentes')
    inputs = frozen.get('review_inputs', {})
    fresh = freeze(frozen['model'], Path(inputs['form']), Path(inputs['account']), Path(inputs['outputs']), corpus_path,
        Path(inputs['cases']))
    if fresh != frozen: raise ValueError('Critérios de revisão/congelamento não satisfeitos')
    return frozen


def freeze(model, form_path, account_path, outputs_path, corpus_path, cases_path=None):
    review = validate_review(json.loads(form_path.read_text()), corpus_path.parent)
    if not review['glossary_accepted']: raise ValueError('Glossário não aceito')
    validate_account(json.loads(account_path.read_text()))
    quality = validate_outputs(json.loads(outputs_path.read_text()), corpus_path)
    if quality['run_fingerprint'] != fingerprint(corpus_path):
        raise ValueError('Saídas de desenvolvimento pertencem a outra versão/configuração')
    model_decisions = [r for r in quality['decisions'] if r['model']==model]
    model_ids = {r['id'] for r in model_decisions}
    if (any(r['automatic_approved'] and (r['critical_error'] or any(r['criteria'][k]['verdict']!='passed'
        for k in ('coverage','support','numbers','citations'))) for r in model_decisions)
        or model_ids.intersection(quality['refinement_regressions'])
        or any(r['errors'] for r in quality['run_rows'] if r['model']==model)):
        raise ValueError('Falhas críticas, regressões ou falhas técnicas impedem congelamento')
    development_sources = {s['id'] for s in json.loads(corpus_path.read_text())['sources'] if s['split']=='development'}
    all_runs = quality['run_rows']
    if any(len({r['repetition'] for r in all_runs if r['source_id']==source and r['model']==candidate}) < 3
           for source in development_sources for candidate in SUPPORTED_MODELS):
        raise ValueError('Comparação dos dois candidatos em três repetições por ata ainda incompleta')
    selected = [r for r in quality['decisions'] if r['model'] == model and r['selected'] and r['split']=='development']
    if any(len({r['repetition'] for r in selected if r['source_id']==source}) < 3 for source in development_sources):
        raise ValueError('Comparação precisa de três repetições por ata de desenvolvimento')
    if any(not any(r['human_passed'] and r['source_id']==source for r in selected) for source in development_sources):
        raise ValueError('Modelo sem saída completa validada em cada ata')
    if not any(r['revision'] > 0 and r['human_passed'] for r in selected):
        raise ValueError('Refinamento em ata completa ainda não validado')
    if not any(after['revision']>0 and after['human_passed'] and any(
        before['result_sha256']==after['result_sha256'] and before['revision']<after['revision'] and not before['human_passed']
        for before in model_decisions) for after in selected):
        raise ValueError('Ganho do refinamento ainda não confirmado pela revisão humana')
    if not cases_path: raise ValueError('Comparação dos casos com rótulos humanos ainda pendente')
    cases = json.loads(cases_path.read_text())
    if (cases.get('fingerprint') != fingerprint(corpus_path)
        or cases.get('review_form_sha256') != digest(form_path)):
        raise ValueError('Comparação de casos pertence a outra política ou revisão')
    expected_ids={r['id'] for r in review['cases'] if r['split']=='development'}
    for candidate in SUPPORTED_MODELS:
        rows=[r for r in cases['rows'] if r['model']==candidate]
        if {r['id'] for r in rows} != expected_ids: raise ValueError('Casos de desenvolvimento incompletos por modelo')
    if any(r['error'] or not r['prediction'] or (r['prediction']['factual_verdict']=='passed'
        and r['human_factual']!='passed') for r in cases['rows'] if r['model']==model):
        raise ValueError('Casos do modelo escolhido têm falha técnica ou falsa aprovação factual')
    return {'version':'m7-freeze-v1', 'status':'frozen', 'model':model, 'fingerprint':fingerprint(corpus_path),
        'review_inputs': {'form':str(form_path.resolve()), 'account':str(account_path.resolve()), 'outputs':str(outputs_path.resolve()),
            'cases':str(cases_path.resolve())},
        'review_artifacts': {str(p.resolve()): digest(p) for p in (form_path, account_path, outputs_path, cases_path)},
        'criteria': {'critical_false_approvals':0, 'refinement_regressions':0, 'technical_failures':0},
        'limitation':'Congelamento libera a reserva; não conclui M7 antes da avaliação final.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare-outputs','quality','freeze'))
    parser.add_argument('--corpus',type=Path,default=Path('tests/fixtures/evaluation/corpus.json'))
    parser.add_argument('--summary',type=Path)
    parser.add_argument('--outputs',type=Path)
    parser.add_argument('--form',type=Path)
    parser.add_argument('--account',type=Path)
    parser.add_argument('--model')
    parser.add_argument('--cases-summary',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.out.exists(): parser.error('Não sobrescrever artefato anterior')
    try:
        if args.action=='prepare-outputs': result=prepare_outputs(args.summary,args.corpus)
        elif args.action=='quality': result=validate_outputs(json.loads(args.outputs.read_text()),args.corpus)
        else: result=freeze(args.model,args.form,args.account,args.outputs,args.corpus,args.cases_summary)
        _save(args.out,result)
    except (ValueError,KeyError,TypeError,AttributeError,OSError) as exc: parser.error(str(exc))
    print(json.dumps({'status':result['status'],'out':str(args.out)}))


if __name__=='__main__': main()
