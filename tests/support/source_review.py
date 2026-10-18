"""Prepara fontes e inventários para testes de avaliação independente."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

from suno_asset.domain.models import Audience, DocumentMetadata, OutputFormat
from suno_asset.services.glossary import GLOSSARY, GLOSSARY_VERSION
from suno_asset.services.parsing import PdfPlumberParser
from suno_asset.services.source_preparation import prepare_source
from suno_asset.m7_review import prepare_codex_cases


INSTRUCTIONS_VERSION = 'm7-codex-instructions-v3'


def _save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def literal_inventory(prepared):
    by_id = {excerpt.excerpt_id: excerpt for excerpt in prepared.excerpts}
    entries, facts = [], []
    patterns = {
        'conditions': r'\b(?:caso|se|condicion\w*|hipótese\w*|cenário\w*)\b',
        'negations': r'\b(?:não|nem|sem)\b',
        'attributions': r'\b(?:Copom|Comitê|Focus|Banco Central|expectativas)\b',
        'modality': r'\b(?:proje\w*|observ\w*|esper\w*|hipótese\w*)\b',
    }
    for block in prepared.blocks:
        excerpts = [by_id[eid].model_dump(mode='json') for eid in block.excerpt_ids]
        context = [by_id[eid].model_dump(mode='json') for eid in block.context_ids]
        text = '\n'.join(row['text'] for row in excerpts)
        fact_id = 'source-' + block.block_id
        facts.append({'id': fact_id, 'block_ids': [block.block_id], 'material': None,
            'statement': text, 'source_reference': [row['ref'] for row in excerpts],
            'support_excerpt': text, 'indicator': None, 'period': None, 'unit': None,
            'conditions': None, 'negations': None, 'attribution': None, 'modality': None,
            'instruction': 'Fato literal candidato. Decompor quando necessário e revisar qualificadores contra a fonte.'})
        entries.append({
            'id': block.block_id, 'kind': block.kind, 'materiality': None,
            'source_excerpts': excerpts, 'context_excerpts': context,
            'literal_markers': {key: [m.group() for m in re.finditer(pattern, text, re.I)]
                                for key, pattern in patterns.items()},
            'facts': [fact_id], 'review_status': 'pending', 'reason': None,
            'instruction': 'Conferir materialidade e decompor em fatos; preservar indicador, período, '
                'unidade, hipóteses, negações e atribuições. Marcadores são pistas, não análise semântica.',
        })
    return {'version': 'm7-source-inventory-v3', 'status': 'pending',
            'origin': 'direct_source_literal_blocks', 'entries': entries, 'facts': facts,
            'limitation': 'Inventário literal integral; fatos atômicos e materialidade aguardam revisão Codex.'}


def prepare_package(corpus_path: Path, out: Path, summaries: tuple[Path, ...] = ()):
    if out.exists():
        raise ValueError('Preserve pacotes anteriores: --out exige uma pasta nova')
    corpus = json.loads(corpus_path.read_text())
    sources = [s for s in corpus['sources'] if s['split'] == 'development']
    if not sources:
        raise ValueError('Nenhuma fonte de desenvolvimento')
    source_map = {s['id']: s for s in sources}
    # Reject heldout runs before opening any result, source, or case file.
    loaded_summaries = [(p, json.loads(p.read_text())) for p in summaries]
    for _, summary in loaded_summaries:
        if summary.get('heldout_included') or any(
            r.get('source_id') not in source_map or r.get('split', 'development') != 'development'
            for r in summary.get('rows', [])):
            raise ValueError('Pacote de desenvolvimento não aceita a reserva')
        if any(r.get('source_sha256') != source_map[r['source_id']]['sha256']
               for r in summary.get('rows', [])):
            raise ValueError('Hash da fonte da rodada diverge do desenvolvimento')
    input_hashes = {}
    inventory_forms = []
    case_forms = []
    for source in sources:
        raw = Path(source['local_pdf']).read_bytes()
        if sha256(raw).hexdigest() != source['sha256']:
            raise ValueError('Hash da fonte divergente: ' + source['id'])
        document_id = uuid5(NAMESPACE_URL, 'm7-handoff:' + source['sha256'])
        document = DocumentMetadata(document_id=document_id, source_id=document_id,
            origin='upload', sha256=source['sha256'], artifact_ref=source['local_pdf'])
        parsed = PdfPlumberParser().parse(raw, document)
        prepared = prepare_source(parsed.units)
        root = out / 'pass-1' / source['id']
        root.mkdir(parents=True)
        (root / 'source.pdf').write_bytes(raw)
        for page in parsed.units:
            (root / f'page-{page.page:02}.txt').write_text(page.text, encoding='utf-8')
        _save(root / 'pages.json', parsed.model_dump(mode='json'))
        _save(root / 'prepared-source.json', prepared.model_dump(mode='json'))
        inventory = literal_inventory(prepared)
        prepared_path = root / 'prepared-source.json'
        inventory.update(review_type='codex', source_id=source['id'], source_sha256=source['sha256'],
            raw_source_file=str((root / 'source.pdf').resolve()),
            prepared_source_file=str(prepared_path.resolve()),
            prepared_source_sha256=sha256(prepared_path.read_bytes()).hexdigest())
        _save(root / 'source-inventory.json', inventory)
        inventory_forms.append({'source_id': source['id'], 'source_sha256': source['sha256'],
            'inventory_file': str((root / 'source-inventory.json').resolve()),
            'entries': [{'id': e['id'], 'materiality': None, 'facts': None,
                         'reason': None} for e in inventory['entries']]})
        case_path = corpus_path.parent / source['case_file']
        cases = json.loads(case_path.read_text())
        if any(c['source_id'] != source['id'] or c['split'] != 'development' for c in cases):
            raise ValueError('Casos fora do desenvolvimento')
        blind = [{k: v for k, v in row.items() if k not in {'expected', 'review'}} for row in cases]
        _save(root / 'cases-blind.json', blind)
        _save(out / 'pass-2' / source['case_file'], cases)
        input_hashes[str(case_path.resolve())] = sha256(case_path.read_bytes()).hexdigest()
        input_hashes[str(Path(source['local_pdf']).resolve())] = source['sha256']
        case_forms.extend({'id': c['id'], 'source_id': source['id'], 'factual_verdict': None,
                           'audience_verdict': None if c['stage'] == 'post_generation' else 'not_applicable',
                           'rationale': None, 'source_reference': None} for c in cases)
    output_forms = []
    for index, (summary_path, summary) in enumerate(loaded_summaries, 1):
        _save(out / 'pass-2' / f'summary-{index}.json', summary)
        input_hashes[str(summary_path.resolve())] = sha256(summary_path.read_bytes()).hexdigest()
        for run_index, run in enumerate(summary.get('rows', []), 1):
            name = f'run-{index:02}-{run_index:03}'
            if not run.get('result_file'):
                _save(out / 'pass-2' / name / 'stage-result.json', run)
                summary_hash = sha256(summary_path.read_bytes()).hexdigest()
                for label in ('before', 'after'):
                    content = run.get(label)
                    if not content:
                        continue
                    revision_id = f'{name}-{label}'
                    _save(out / 'pass-1' / run['source_id'] / (revision_id + '.json'), content)
                    output_forms.append({'id': revision_id, 'source_id': run['source_id'],
                        'result_sha256': summary_hash, 'scope': 'controlled_refinement',
                        'revision': content['revision'], 'audience': content['audience'],
                        'output_format': content['output_format'], 'facts': None,
                        'unsupported_claims': None, 'numeric_errors': None, 'attribution_errors': None,
                        'false_approvals': None, 'false_rejections': None,
                        'divergences': None, 'verdict': None, 'reason': None})
                if run.get('audit_dir'):
                    for audit in sorted(Path(run['audit_dir']).glob('*.json')):
                        _save(out / 'pass-2' / name / 'call-audit' / audit.name, json.loads(audit.read_text()))
                continue
            path = Path(run['result_file'])
            digest = sha256(path.read_bytes()).hexdigest()
            if digest != run['result_sha256']:
                raise ValueError('Resultado alterado desde a rodada')
            result = json.loads(path.read_text())
            _save(out / 'pass-2' / name / 'result.json', result)
            input_hashes[str(path.resolve())] = digest
            for revision in result.get('history', []):
                if not revision.get('content'):
                    continue
                revision_id = f'{name}-revision-{revision["number"]}'
                _save(out / 'pass-1' / run['source_id'] / (revision_id + '.json'), revision['content'])
                output_forms.append({'id': revision_id, 'source_id': run['source_id'],
                    'result_sha256': digest, 'revision': revision['number'],
                    'audience': revision['content']['audience'],
                    'output_format': revision['content']['output_format'],
                    'facts': None, 'unsupported_claims': None, 'numeric_errors': None,
                    'attribution_errors': None, 'false_approvals': None, 'false_rejections': None,
                    'divergences': None, 'verdict': None, 'reason': None})
            # Copy complete trace artifacts; never copy .env or credentials.
            for trace in sorted(path.parent.glob(path.stem + '-*.json')):
                _save(out / 'pass-2' / name / trace.name, json.loads(trace.read_text()))
            audit_dir = Path(run['audit_dir']) if run.get('audit_dir') else path.parent / 'call-audit'
            for audit in sorted(audit_dir.glob('*.json')):
                _save(out / 'pass-2' / name / 'call-audit' / audit.name, json.loads(audit.read_text()))
    common = {'version': 'm7-codex-review-v3', 'review_type': 'codex', 'status': 'pending',
        'session_id': None, 'model': None, 'reviewed_at': None,
        'instructions_version': INSTRUCTIONS_VERSION, 'input_hashes': input_hashes,
        'passes': {'source_first': False, 'pipeline_comparison': False},
        'unresolved_divergences': None,
        'limitations': ['Revisão pendente; ausência de informação não equivale a zero.']}
    _save(out / 'forms' / 'source-inventory.json', {**common, 'sources': inventory_forms})
    _save(out / 'forms' / 'cases-and-glossary.json', {
        **prepare_codex_cases(corpus_path.parent), **common})
    _save(out / 'pass-1' / 'glossary.json', {'version': GLOSSARY_VERSION, 'entries': GLOSSARY})
    _save(out / 'forms' / 'outputs.json', {**common, 'rows': output_forms})
    _save(out / 'forms' / 'refinement.json', {**common, 'rows': [],
        'instruction': 'Preparar formulário validável a partir da rodada real de refinamento; nunca inventar resultados.'})
    _save(out / 'forms' / 'matrix.json', {**common, 'rows': [
        {'source_id': s['id'], 'audience': audience, 'output_format': fmt,
         'result_sha256': None, 'verdict': None, 'reason': None}
        for s in sources for audience in (a.value for a in Audience)
        for fmt in (f.value for f in OutputFormat)]})
    report = {'version': 'm7-codex-handoff-v3', 'paid': False,
        'prepared_at': datetime.now(timezone.utc).isoformat(),
        'source_ids': list(source_map), 'case_count': len(case_forms),
        'revision_count': len(output_forms), 'heldout_included': False,
        'source_inventory_status': 'pending', 'input_hashes': input_hashes,
        'package_hashes': {str(p.relative_to(out)): sha256(p.read_bytes()).hexdigest()
                           for p in sorted(out.rglob('*')) if p.is_file()},
        'limitation': 'Preparação offline. Nenhuma revisão ou rodada real foi aprovada.'}
    _save(out / 'manifest.json', report)
    return report
