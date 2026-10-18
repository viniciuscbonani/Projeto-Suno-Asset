"""Valida revisão preenchida por uma pessoa, preservando os rótulos propostos."""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from suno_asset.services.glossary import GLOSSARY_VERSION, GLOSSARY

VERDICTS = {'passed', 'failed', 'not_verified'}
CODEX_REVIEW_VERSION = 'm7-codex-review-v3'


def codex_metadata(*, instructions_version='m7-codex-instructions-v3'):
    """Unknown identity stays unknown; preparation never fabricates a review."""
    return {'review_type': 'codex', 'session_id': None, 'model': None,
        'instructions_version': instructions_version, 'limitations': [],
        'passes': {'source_first': False, 'pipeline_comparison': False},
        'unresolved_divergences': None}


def validate_codex_identity(form, time_field='reviewed_at'):
    if form.get('status') != 'reviewed' or form.get('review_type') != 'codex':
        raise ValueError('Revisão Codex pendente')
    timestamp = datetime.fromisoformat(form.get(time_field) or '')
    if timestamp.utcoffset() is None:
        raise ValueError('Data da revisão precisa conter fuso')
    if not isinstance(form.get('instructions_version'), str) or not form['instructions_version'].strip():
        raise ValueError('Versão das instruções ausente')
    for field in ('session_id', 'model'):
        if form.get(field) is not None and (not isinstance(form[field], str) or not form[field].strip()):
            raise ValueError('Identidade desconhecida deve permanecer null')
    if not isinstance(form.get('limitations'), list) or not form['limitations']:
        raise ValueError('Limitações da revisão não registradas')
    if form.get('passes') != {'source_first': True, 'pipeline_comparison': True}:
        raise ValueError('Duas passagens independentes de revisão pendentes')
    if form.get('unresolved_divergences') != []:
        raise ValueError('Divergências sem resolução impedem aceite')


def validate_inventories(form, sources):
    inventories = form.get('inventories') or {}
    if set(inventories) != set(sources):
        raise ValueError('Inventários independentes ausentes por fonte')
    material_ids = {}
    for source_id, source_hash in sources.items():
        reference = inventories[source_id]
        path = Path(reference['file'])
        if sha256(path.read_bytes()).hexdigest() != reference.get('sha256'):
            raise ValueError('Inventário independente alterado')
        inventory = json.loads(path.read_text())
        if (inventory.get('version') != 'm7-source-inventory-v3' or inventory.get('status') != 'reviewed' or inventory.get('review_type') != 'codex'
            or inventory.get('source_id') != source_id or inventory.get('source_sha256') != source_hash):
            raise ValueError('Inventário elaborado da fonte ainda não revisado')
        facts = inventory.get('facts') or []
        if (not facts or len({f['id'] for f in facts}) != len(facts)
            or any(type(f.get('material')) is not bool or not f.get('source_reference') or not f.get('support_excerpt') for f in facts)):
            raise ValueError('Inventário material incompleto/duplicado')
        entries = inventory.get('entries') or []
        if inventory.get('version') == 'm7-source-inventory-v3':
            prepared_path = Path(inventory['prepared_source_file'])
            if sha256(prepared_path.read_bytes()).hexdigest() != inventory.get('prepared_source_sha256'):
                raise ValueError('Fonte preparada do inventário alterada')
            prepared = json.loads(prepared_path.read_text())
            from suno_asset.domain.models import DocumentMetadata, PreparedSource
            from suno_asset.services.parsing import PdfPlumberParser
            source = PreparedSource.model_validate(prepared)
            raw_path = Path(inventory['raw_source_file'])
            raw = raw_path.read_bytes()
            if sha256(raw).hexdigest() != source_hash:
                raise ValueError('PDF original do inventário alterado')
            metadata = DocumentMetadata(document_id=source.document_id, source_id=source.document_id,
                origin='upload', sha256=source_hash, artifact_ref=str(raw_path))
            parsed = PdfPlumberParser().parse(raw, metadata)
            if source.units != parsed.units:
                raise ValueError('Fonte preparada não corresponde ao texto/offsets do PDF original')
            blocks = prepared['blocks']
            if len(entries) != len(blocks) or {e['id'] for e in entries} != {b['block_id'] for b in blocks}:
                raise ValueError('Inventário precisa conter todos os blocos originais da fonte')
            originals = {b['block_id']:b for b in blocks}
            excerpts = {e['excerpt_id']:e for e in prepared['excerpts']}
            if any(e.get('source_excerpts') != [excerpts[i] for i in originals[e['id']]['excerpt_ids']]
                or e.get('context_excerpts') != [excerpts[i] for i in originals[e['id']].get('context_ids',[])] for e in entries):
                raise ValueError('Trechos/offsets originais do inventário alterados')
            if not entries or any(type(e.get('materiality')) is not bool or e.get('review_status') != 'reviewed'
                or (not e['materiality'] and not e.get('reason')) for e in entries):
                raise ValueError('Todas entradas da fonte exigem revisão e descarte administrativo justificado')
            material_blocks = {e['id'] for e in entries if e['materiality']}
            covered_blocks = {b for f in facts if f.get('material') for b in f.get('block_ids', [])}
            if not material_blocks <= covered_blocks:
                raise ValueError('Fato material de apoio não pode ser retirado do inventário')
        material_ids[source_id] = {f['id'] for f in facts if f['material']}
        if not material_ids[source_id]: raise ValueError('Inventário sem fatos materiais')
    return material_ids


def prepare_codex_cases(directory: Path, *, split='development'):
    corpus = json.loads((directory / 'corpus.json').read_text())
    sources = [s for s in corpus['sources'] if s['split'] == split]
    proposals = [row for source in sources
        for row in json.loads((directory / source['case_file']).read_text())]
    return {'version': CODEX_REVIEW_VERSION, 'status': 'pending', **codex_metadata(),
        'reviewed_at': None, 'split': split,
        'source_hashes': {s['id']: s['sha256'] for s in sources},
        'dataset_hashes': {s['case_file']: sha256((directory / s['case_file']).read_bytes()).hexdigest() for s in sources},
        'cases': [{'id': r['id'], 'source_id': r['source_id'], 'factual_verdict': None,
            'audience_verdict': None if r['stage'] == 'post_generation' else 'not_applicable',
            'rationale': None, 'source_reference': None, 'support_excerpt': None,
            'divergence_resolution': None} for r in proposals],
        'glossary_version': GLOSSARY_VERSION,
        'glossary': [{'term': e['term'], 'accepted': None, 'comment': None} for e in GLOSSARY]}


def validate_review(form: dict, directory: Path) -> dict:
    if form.get('version') == CODEX_REVIEW_VERSION:
        return validate_codex_cases(form, directory)
    corpus = json.loads((directory / 'corpus.json').read_text())
    reviewer = form.get('reviewer')
    if not isinstance(reviewer, str) or not reviewer.strip() or form.get('status') != 'reviewed':
        raise ValueError('Revisão humana pendente: preencher responsável e status reviewed')
    reviewed_at = datetime.fromisoformat(form.get('reviewed_at') or '')
    if reviewed_at.utcoffset() is None:
        raise ValueError('Data da revisão precisa conter fuso')
    proposals = {}
    for source in corpus['sources']:
        if form.get('source_hashes', {}).get(source['id']) != source['sha256']:
            raise ValueError('Fonte alterada desde a preparação do formulário')
        path = directory / source['case_file']
        if form.get('dataset_hashes', {}).get(source['case_file']) != sha256(path.read_bytes()).hexdigest():
            raise ValueError('Casos alterados desde a preparação do formulário')
        proposals.update({row['id']: row for row in json.loads(path.read_text())})
    decisions = form.get('cases') or []
    if len(decisions) != len(proposals) or {row['id'] for row in decisions} != set(proposals):
        raise ValueError('Revisão incompleta, duplicada ou com IDs desconhecidos')
    adjudications = []
    for row in decisions:
        proposal = proposals[row['id']]
        post = proposal['stage'] == 'post_generation'
        if row.get('source_id') != proposal['source_id'] or row.get('factual_verdict') not in VERDICTS:
            raise ValueError(f'Fonte ou julgamento factual inválido: {row["id"]}')
        if row.get('audience_verdict') not in (VERDICTS if post else {'not_applicable'}):
            raise ValueError(f'Julgamento de público inválido: {row["id"]}')
        if not isinstance(row.get('rationale'), str) or not row['rationale'].strip():
            raise ValueError(f'Justificativa ausente: {row["id"]}')
        dimensions = [row['factual_verdict']] + ([row['audience_verdict']] if post else [])
        combined = 'failed' if 'failed' in dimensions else 'not_verified' if 'not_verified' in dimensions else 'passed'
        agreement = (row['factual_verdict'] == proposal['expected']['factual_verdict'] and
                     (not post or row['audience_verdict'] == proposal['expected']['audience_verdict']))
        adjudications.append({**row, 'verdict': combined, 'draft_agreement': agreement,
            'proposed': proposal['expected'], 'split': proposal['split']})
    glossary = form.get('glossary') or []
    if (form.get('glossary_version') != GLOSSARY_VERSION or len(glossary) != len(GLOSSARY)
        or {entry['term'] for entry in glossary} != {entry['term'] for entry in GLOSSARY}
        or any(type(entry.get('accepted')) is not bool or not entry.get('comment') for entry in glossary)):
        raise ValueError('Revisão do glossário incompleta ou versão divergente')
    return {'version': 'm7-human-review-v1', 'status': 'reviewed', 'reviewer': reviewer,
        'reviewed_at': reviewed_at.isoformat(), 'dataset_hashes': form['dataset_hashes'],
        'source_hashes': form['source_hashes'],
        'cases': adjudications, 'disagreements': [row['id'] for row in adjudications if not row['draft_agreement']],
        'glossary_version': GLOSSARY_VERSION, 'glossary': glossary,
        'glossary_accepted': all(entry['accepted'] for entry in glossary),
        'limitation': 'Uma pessoa revisora; não mede acurácia de modelo nem conclui M7.'}


def validate_codex_cases(form, directory):
    validate_codex_identity(form)
    split = form.get('split')
    if split not in {'development', 'heldout'}:
        raise ValueError('Split da revisão inválido')
    expected = prepare_codex_cases(directory, split=split)
    for key in ('source_hashes', 'dataset_hashes', 'glossary_version'):
        if form.get(key) != expected[key]:
            raise ValueError('Hashes/versão alterados desde a preparação')
    originals = {r['id']: r for r in expected['cases']}
    rows = form.get('cases') or []
    if len(rows) != len(originals) or {r['id'] for r in rows} != set(originals):
        raise ValueError('Revisão Codex incompleta ou duplicada')
    corpus = json.loads((directory / 'corpus.json').read_text())
    proposals = {r['id']: r for s in corpus['sources'] if s['split'] == split
        for r in json.loads((directory / s['case_file']).read_text())}
    decisions = []
    for row in rows:
        original, proposal = originals[row['id']], proposals[row['id']]
        if (row.get('source_id') != original['source_id'] or row.get('factual_verdict') not in VERDICTS
            or row.get('audience_verdict') not in (VERDICTS if proposal['stage'] == 'post_generation' else {'not_applicable'})):
            raise ValueError('Parecer Codex inválido')
        if any(not isinstance(row.get(key), str) or not row[key].strip()
               for key in ('rationale', 'source_reference', 'support_excerpt')):
            raise ValueError('Justificativa/suporte exato ausente')
        if row['factual_verdict']=='not_verified' and proposal.get('evidence_availability')!='unavailable' and not proposal.get('evidence_override'):
            raise ValueError('Julgamento factual inconclusivo fora de controle de evidência indisponível')
        agreement = row['factual_verdict'] == proposal['expected']['factual_verdict'] and (
            proposal['stage'] != 'post_generation' or row['audience_verdict'] == proposal['expected']['audience_verdict'])
        if not agreement and not row.get('divergence_resolution'):
            raise ValueError('Divergência com pipeline sem resolução')
        dimensions = [row['factual_verdict']] + ([row['audience_verdict']] if proposal['stage'] == 'post_generation' else [])
        decisions.append({**row, 'split': split, 'draft_agreement': agreement,
            'verdict': 'failed' if 'failed' in dimensions else 'not_verified' if 'not_verified' in dimensions else 'passed'})
    glossary = form.get('glossary') or []
    if (len(glossary) != len(GLOSSARY) or {e['term'] for e in glossary} != {e['term'] for e in GLOSSARY}
        or any(type(e.get('accepted')) is not bool or not e.get('comment') for e in glossary)):
        raise ValueError('Revisão Codex do glossário incompleta')
    return {**form, 'cases': decisions, 'glossary_accepted': all(e['accepted'] for e in glossary)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--form', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--evals', type=Path, default=Path('tests/fixtures/evaluation'))
    parser.add_argument('--prepare-codex', action='store_true')
    parser.add_argument('--allow-heldout', action='store_true')
    parser.add_argument('--frozen', type=Path)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('Não sobrescrever revisão anterior')
    try:
        if args.allow_heldout:
            if not args.frozen: raise ValueError('Reserva só liberada após congelamento')
            from suno_asset.m7_literal_acceptance import validate_frozen_configuration
            frozen = json.loads(args.frozen.read_text())
            validate_frozen_configuration(args.frozen,args.evals/'corpus.json',frozen['configuration']['id'])
            from suno_asset.m7_promotion import reserve_ledger_path
            ledger_path=reserve_ledger_path(args.evals/'corpus.json')
            ledger=json.loads(ledger_path.read_text())
            if (ledger.get('status')!='started' or ledger.get('frozen_sha256')!=sha256(args.frozen.read_bytes()).hexdigest()
                or ledger_path.with_name(ledger_path.stem+'-correction.json').exists()):
                raise ValueError('Revisão da reserva exige registro de lote único iniciado e não contaminado')
        report = prepare_codex_cases(args.evals,split='heldout' if args.allow_heldout else 'development') if args.prepare_codex else validate_review(json.loads(args.form.read_text()), args.evals)
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'{len(report["cases"])} casos; estado {report["status"]}.')


if __name__ == '__main__': main()
