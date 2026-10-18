"""Conservative literal rules; unresolved text always remains available to the LLM."""
from __future__ import annotations

from datetime import date
import re

from suno_asset.experiments.literal_efficiency.contracts import normalize_model_facts

RULES_VERSION = 'hybrid-literal-rules-v1'
MONTHS = dict(zip(('janeiro fevereiro março abril maio junho julho agosto setembro outubro novembro dezembro').split(), range(1, 13)))
DECISION = re.compile(r'(?is)^\s*(?:\d+[.)]\s*)?(?:O\s+)?(?:Copom|Comitê)\s+decidiu\s+(?:reduzir|elevar|manter|fixar)\b.*\b(?:Selic|taxa\s+básica\s+de\s+juros)\b')
HYPOTHETICAL = re.compile(r'(?i)\b(?:se|caso|poderia|talvez|hipótese|hipotético|recomendou|não decidiu)\b')
GUIDANCE = re.compile(r'(?is)\b(?:o comitê|o copom)\b.*\b(?:seguirá incorporando|continuará acompanhando|magnitude do ciclo de calibração será ajustada)\b')
DATE = re.compile(r'(?i)^\s*Data\s*:\s*(\d{1,2})(?:\s+(?:e|a)\s+(\d{1,2}))?\s+de\s+([a-zç]+)\s+de\s+(\d{4})\s*[.]?\s*$')


def sentences(text):
    # A decimal, an item number or a.a. is not a sentence boundary.
    return tuple(part.strip() for part in re.split(r'(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ])', text) if part.strip())


def administrative_reason(block, excerpts):
    text = ' '.join(' '.join(excerpts[eid].text.split()) for eid in block.excerpt_ids)
    if re.fullmatch(r'\d+\s+bcb\.gov\.br', text, re.I):
        return 'literal_page_footer'
    if re.fullmatch(r'\d+ª\s+Reunião\s+COPOM\s+\d{1,2}(?:\s+e\s+\d{1,2})?\s+de\s+[a-zç]+', text, re.I):
        return 'literal_repeated_meeting_header'
    if re.fullmatch(r'\d+ª\s+Ata da Reunião do\s+Comitê de Política Monetária\s*[—–-]\s*Copom', text, re.I):
        return 'literal_document_title'
    # Section headings, footnotes and mixed administrative/economic paragraphs
    # are NOT excluded on the basis of kind or absence of keywords.
    return None


def rule_extract(source, source_sha256):
    by_id = {e.excerpt_id: e for e in source.excerpts}
    facts, metadata, administrative = [], {}, {}
    for block in source.blocks:
        reason = administrative_reason(block, by_id)
        if reason:
            administrative[block.block_id] = reason
            continue
        candidates = []
        for eid in block.excerpt_ids:
            text = by_id[eid].text
            normalized = ' '.join(text.split())
            match = DATE.fullmatch(normalized)
            if match and match[3].lower() in MONTHS:
                try:
                    first = date(int(match[4]), MONTHS[match[3].lower()], int(match[1]))
                    last = date(int(match[4]), first.month, int(match[2] or match[1]))
                    if first > last:
                        raise ValueError('intervalo invertido')
                    candidates.append(dict(tipo='date', texto_literal=text.strip(), apoio_ids=[eid]))
                except ValueError:
                    pass
            for sentence in sentences(text):
                flat = ' '.join(sentence.split())
                complete = sentence.endswith(('.', '!', '?'))
                if (complete and DECISION.search(flat) and not HYPOTHETICAL.search(flat)
                    and len(re.findall(r'\d+(?:[.,]\d+)?\s*%', flat)) == 1):
                    candidates.append(dict(tipo='decision', texto_literal=sentence, apoio_ids=[eid]))
                if complete and GUIDANCE.search(flat) and not HYPOTHETICAL.search(flat):
                    # Keep the COMPLETE sentence, never the phrase triggering the rule.
                    candidates.append(dict(tipo='outlook', texto_literal=sentence, apoio_ids=[eid]))
                if (complete and re.match(r'(?i)^(?:\d+[.)]\s*)?Votaram por essa decisão os seguintes membros do Comitê:', flat)
                    and not HYPOTHETICAL.search(flat)
                    and any(f.tipo == 'decision' for f in facts)):
                    # Preserve the named vote, without inferring unanimity or a count.
                    candidates.append(dict(tipo='rationale', texto_literal=sentence, apoio_ids=[eid]))
            support_ids = tuple(dict.fromkeys((eid, *block.required_context_ids)))
            context = ' '.join(' '.join(by_id[x].text.split()) for x in support_ids)
            header = re.search(r'Índice de preços\s+(\d{4})\s+(\d{4})\s+([1-4]º\s+tri\s+\d{4})\s*\n', text, re.I)
            table_rows = text[header.end():].strip().splitlines() if header else []
            row_pattern = r'IPCA(?: livres| administrados)?[ \t]+\d+[,.]\d+(?:[ \t]+\d+[,.]\d+){2}[ \t]*'
            table_proven = (len(table_rows)==3 and all(re.fullmatch(row_pattern,row) for row in table_rows)
                and len({re.split(r'\s+\d',row,maxsplit=1)[0] for row in table_rows})==3)
            if (header and 'Projeções de inflação no cenário de referência' in context
                and re.search(r'Variação do IPCA acumulada em quatro trimestres\s*\(%\)', text, re.I)
                and table_proven
                and not any('unresolved' in d or 'ambiguous' in d for d in block.diagnostics if d!='ambiguous_table_layout')
                and len(set(header.groups())) == 3):
                for row in re.finditer(r'(?m)^IPCA(?: livres| administrados)?\s+\d+[,.]\d+(?:[ \t]+\d+[,.]\d+){2}[ \t]*$', text):
                    candidates.append(dict(tipo='number', texto_literal=row.group(), apoio_ids=list(support_ids)))
        if candidates:
            normalized_facts = normalize_model_facts(source, block.block_id, candidates,
                source_sha256=source_sha256, provenance='rule')
            facts.extend(normalized_facts)
            for fact in normalized_facts:
                metadata[fact.id] = {'rule_version': RULES_VERSION, 'owner_block_id': block.block_id,
                    'whole_sentence_or_table_row': True, 'semantic_verification_required': True,
                    'table_mapping':'full_three_column_reference_scenario_grammar' if fact.tipo=='number' else None}
    return tuple(facts), metadata, administrative


def unresolved_ranges(source, facts, administrative):
    result = {}
    for block in source.blocks:
        if block.block_id in administrative:
            continue
        for eid in block.excerpt_ids:
            e = next(e for e in source.excerpts if e.excerpt_id == eid)
            spans = sorted((f.start - e.ref.start, f.end - e.ref.start) for f in facts
                if f.owner_block_id == block.block_id and f.page == e.ref.page and e.ref.start <= f.start < f.end <= e.ref.end
                and eid in f.apoio_ids)
            cursor, ranges = 0, []
            for start, end in spans:
                if e.text[cursor:start].strip():
                    ranges.append([cursor, start])
                cursor = max(cursor, end)
            if e.text[cursor:].strip():
                ranges.append([cursor, len(e.text)])
            if ranges:
                result[eid] = ranges
    return result
