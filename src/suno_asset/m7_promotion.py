"""Reserva de uso único, aceite final e ativação condicionada do vencedor M7."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from suno_asset.m7_acceptance import digest, fingerprint, ROOT, validate_trace_hashes, technical_attempts_complete, provider_calls_are_real
from suno_asset.m7_eval import _save
from suno_asset.m7_literal_acceptance import validate_frozen_configuration, validate_matrix_review
from suno_asset.m7_review import validate_review

RESERVE_REGISTRY = ROOT / 'data' / 'm7-reserve-registry'


def reserve_ledger_path(corpus_path):
    corpus = json.loads(corpus_path.read_text())
    reserve = [s for s in corpus['sources'] if s['split'] == 'heldout']
    if len(reserve) != 1:
        raise ValueError('Uma reserva independente exigida por lote')
    return RESERVE_REGISTRY / ('reserve-use-' + reserve[0]['sha256'] + '.json')


def begin_reserve(frozen_path, corpus_path, name, out):
    """Claim by source hash, independently of frozen filename or candidate."""
    frozen = validate_frozen_configuration(frozen_path, corpus_path, name)
    path = reserve_ledger_path(corpus_path)
    value = {'version':'m7-reserve-use-v3', 'status':'started',
        'started_at':datetime.now(timezone.utc).isoformat(),
        'frozen_file':str(frozen_path.resolve()), 'frozen_sha256':digest(frozen_path),
        'configuration':frozen['configuration'], 'out':str(out.resolve()),
        'source_hashes':{s['id']:s['sha256'] for s in json.loads(corpus_path.read_text())['sources'] if s['split']=='heldout'},
        'correction_guided_by_reserve':False}
    try:
        _save(path, value)
    except ValueError as exc:
        raise ValueError('Reserva já usada/iniciada; correção exige nova reserva independente') from exc
    return path


def mark_reserve_consumed(corpus_path, reason):
    """A correction guided by this source permanently prevents its acceptance."""
    if not isinstance(reason, str) or not reason.strip(): raise ValueError('Motivo da correção obrigatório')
    path = reserve_ledger_path(corpus_path)
    ledger = json.loads(path.read_text())
    ledger.update(status='consumed_for_correction', correction_guided_by_reserve=True, correction_reason=reason)
    # Preserve the original claim as well as the explicit correction record.
    _save(path.with_name(path.stem + '-correction.json'), ledger)
    return ledger


def accept_reserve(frozen_path, matrix_path, cases_review_path, cases_summary_path, corpus_path):
    frozen_data = json.loads(frozen_path.read_text())
    name = frozen_data['configuration']['id']
    frozen = validate_frozen_configuration(frozen_path, corpus_path, name)
    ledger_path = reserve_ledger_path(corpus_path)
    ledger = json.loads(ledger_path.read_text())
    if (ledger_path.with_name(ledger_path.stem + '-correction.json').exists()
        or ledger.get('correction_guided_by_reserve') is not False
        or ledger.get('frozen_sha256') != digest(frozen_path) or ledger.get('status') != 'started'):
        raise ValueError('Reserva reutilizada/contaminada ou congelamento divergente; exigir nova reserva')
    matrix = validate_matrix_review(json.loads(matrix_path.read_text()), corpus_path, name, split='heldout')
    summary_path = Path(matrix['summary_file'])
    summary = json.loads(summary_path.read_text())
    if (summary_path.parent.resolve() != Path(ledger['out']).resolve()
        or summary.get('reserve_ledger_sha256') != digest(ledger_path)
        or summary.get('frozen_sha256') != digest(frozen_path)):
        raise ValueError('Lote reserva não vinculado ao registro de uso único/congelamento')
    review = validate_review(json.loads(cases_review_path.read_text()), corpus_path.parent)
    if review.get('version') != 'm7-codex-review-v3' or review.get('split') != 'heldout' or len(review['cases']) != 15:
        raise ValueError('Revisão Codex dos 15 casos reserva pendente')
    cases = json.loads(cases_summary_path.read_text())
    cases_marker = ledger_path.with_name(ledger_path.stem+'-cases.json')
    claim = json.loads(cases_marker.read_text())
    if (Path(claim['out']).resolve() != cases_summary_path.parent.resolve()
        or claim.get('frozen_sha256') != digest(frozen_path) or claim.get('reserve_ledger_sha256') != digest(ledger_path)):
        raise ValueError('Casos reserva não vinculados ao lote único')
    if (not provider_calls_are_real(cases) or cases.get('fingerprint') != frozen['fingerprint'] or cases.get('review_form_sha256') != digest(cases_review_path)
        or cases.get('frozen_sha256') != digest(frozen_path) or cases.get('reserve_ledger_sha256') != digest(ledger_path)):
        raise ValueError('Comparação de casos reserva de outra revisão/congelamento/lote')
    rows = cases.get('rows') or []
    from suno_asset.m7_cases import source_pages
    from suno_asset.m7_acceptance import validate_case_execution
    reserve_sources = [s for s in json.loads(corpus_path.read_text())['sources'] if s['split']=='heldout']
    case_proposals = {case['id']:case for source in reserve_sources
        for case in json.loads((corpus_path.parent/source['case_file']).read_text())}
    pages_by_source = {source['id']:source_pages(source) for source in reserve_sources}
    for row in rows:
        validate_trace_hashes(row)
        validate_case_execution(row, case_proposals[row['id']], pages_by_source[row['source_id']], frozen['configuration'])
    labels = {r['id']:r for r in review['cases']}
    if (len(rows) != 15 or {r['id'] for r in rows} != {r['id'] for r in review['cases']}
        or any(r.get('configuration_id') != name or r.get('error') or not r.get('prediction') or (
            r['prediction']['factual_verdict'] == 'passed' and r.get('human_factual') != 'passed')
            or not technical_attempts_complete(r) for r in rows)):
        raise ValueError('Reserva com casos incompletos/falsa aprovação factual')
    if any(r.get('human_factual') != labels[r['id']]['factual_verdict']
        or r.get('human_audience') != labels[r['id']]['audience_verdict']
        or r.get('source_id') != labels[r['id']]['source_id']
        or r.get('configuration') != frozen['configuration'] for r in rows):
        raise ValueError('Rótulos/configuração dos casos reserva alterados')
    paths = {'frozen':frozen_path,'matrix':matrix_path,'cases_review':cases_review_path,
        'cases_summary':cases_summary_path,'ledger':ledger_path,'cases_marker':cases_marker}
    return {'version':'m7-final-acceptance-v3', 'status':'accepted', 'configuration':frozen['configuration'],
        'fingerprint':frozen['fingerprint'], 'accepted_at':datetime.now(timezone.utc).isoformat(),
        'inputs':{k:str(p.resolve()) for k,p in paths.items()},
        'artifact_hashes':{str(p.resolve()):digest(p) for p in paths.values()},
        'reserve_runs':9,'reserve_cases':15,'correction_guided_by_reserve':False,
        'limitation':'Validação somente em atas públicas; retenção da conta não autoriza uso privado.'}


def promote(frozen_path, acceptance_path, corpus_path):
    accepted = json.loads(acceptance_path.read_text())
    if accepted.get('version') != 'm7-final-acceptance-v3' or accepted.get('status') != 'accepted':
        raise ValueError('Promoção antecipada: aceite final v3 ausente')
    for artifact, expected in accepted.get('artifact_hashes', {}).items():
        if digest(artifact) != expected: raise ValueError('Aceite/artefatos alterados')
    args = accepted.get('inputs') or {}
    if args.get('frozen') != str(frozen_path.resolve()): raise ValueError('Congelamento do aceite divergente')
    fresh = accept_reserve(frozen_path, *(Path(args[k]) for k in ('matrix','cases_review','cases_summary')), corpus_path)
    fresh['accepted_at'] = accepted['accepted_at']
    if fresh != accepted: raise ValueError('Aceite final inválido')
    spec = accepted['configuration']
    return {'version':'m7-promotion-v3','status':'promoted','configuration':spec,
        'corpus_file':str(corpus_path.resolve()),
        'architecture':spec['architecture'],'model_profile':spec['profile'],'configuration_id':spec['id'],
        'promoted_at':datetime.now(timezone.utc).isoformat(),
        'frozen_file':str(frozen_path.resolve()),'frozen_sha256':digest(frozen_path),
        'acceptance_file':str(acceptance_path.resolve()),'acceptance_sha256':digest(acceptance_path),
        'code_hashes':accepted['fingerprint']['code_hashes']}


def load_promoted_configuration(path: Path | None):
    if path is None: return None
    promoted = json.loads(path.read_text())
    if promoted.get('version') != 'm7-promotion-v3' or promoted.get('status') != 'promoted':
        raise ValueError('Configuração promovida inválida')
    for field in ('frozen','acceptance'):
        if digest(promoted[field+'_file']) != promoted[field+'_sha256']:
            raise ValueError('Artefatos de promoção alterados')
    if not promoted.get('code_hashes') or any(digest(ROOT / f) != h for f,h in promoted['code_hashes'].items()):
        raise ValueError('Código alterado após validação da reserva')
    accepted = json.loads(Path(promoted['acceptance_file']).read_text())
    if accepted.get('status') != 'accepted' or accepted.get('configuration') != promoted['configuration']:
        raise ValueError('Aceite da promoção inválido')
    for artifact, expected in accepted.get('artifact_hashes', {}).items():
        if digest(artifact) != expected: raise ValueError('Artefatos de aceite alterados')
    ledger = Path(accepted['inputs']['ledger'])
    if ledger.with_name(ledger.stem+'-correction.json').exists(): raise ValueError('Reserva orientou correção; nova reserva exigida')
    # File hashes prove integrity, not eligibility. Recompute every release gate.
    fresh = promote(Path(promoted['frozen_file']),Path(promoted['acceptance_file']),Path(promoted['corpus_file']))
    fresh['promoted_at'] = promoted['promoted_at']
    if fresh != promoted: raise ValueError('Promoção não reproduz os critérios completos de aceite')
    return promoted['configuration']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('accept','promote','invalidate-reserve'))
    parser.add_argument('--corpus', type=Path, default=Path('tests/fixtures/evaluation/corpus.json'))
    for name in ('frozen','matrix','cases-review','cases-summary','acceptance','out'):
        parser.add_argument('--'+name, type=Path, required=name=='out')
    parser.add_argument('--reason')
    args = parser.parse_args()
    try:
        if args.action == 'accept':
            result = accept_reserve(args.frozen,args.matrix,args.cases_review,args.cases_summary,args.corpus)
        elif args.action == 'promote': result = promote(args.frozen,args.acceptance,args.corpus)
        else: result = mark_reserve_consumed(args.corpus,args.reason)
        _save(args.out, result)
    except (ValueError,KeyError,TypeError,AttributeError,OSError) as exc: parser.error(str(exc))
    print(json.dumps({'status':result['status'],'out':str(args.out)}))


if __name__ == '__main__': main()
