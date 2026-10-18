"""Segmentação literal: offsets/IDs locais, propriedade única e contexto explícito."""
from __future__ import annotations

from hashlib import sha256
import re

from suno_asset.domain.models import (Chunk, Evidence, EvidenceRef, ParsedUnit,
    PreparedSource, SourceBlock, SourceExcerpt)
from suno_asset.services.llm_budget import TokenCounter
from suno_asset.services.protocols import ExpectedServiceError

SEGMENTATION_VERSION = 'logical-source-v1.4'
TARGET_SOURCE_TOKENS = 1000
MAX_SOURCE_TOKENS = 2000
_NUMBERED = re.compile(r'^\s*\d{1,3}[.)]\s+\S')
_TABLE = re.compile(r'(?i)^\s*(?:tabela\b|índice de preços|variação.*\(%\)|IPCA(?:\s+livres|\s+administrados)?\s+\d)|\S\s{2,}\S.*\d|\|')
_NOTE = re.compile(r'(?i)^\s*(?:nota|fonte|obs|\d{1,2}[/-]|\*|\d{1,2}\s+[A-ZÁÉÍÓÚ])')
_HEADING = re.compile(r'^\s*(?:[A-Z][.)]\s+|[A-ZÁÉÍÓÚ][A-ZÁÉÍÓÚÇÂÊÔÃÕ0-9 /–—-]{2,90}$|(?:Projeções|Histórico|Participantes|Data|Local)\b[^.!?]{0,80}$)')
_SEMANTIC_HEADING = re.compile(r'(?i)projeç(?:ão|ões)|hipóteses?|cenário de referência|expectativas? de inflação')
_NOTE_DEFINITION = re.compile(r'(?m)^[ \t]*([1-9]\d?)[ \t]+[A-ZÁÉÍÓÚ]')
_NOTE_REFERENCE = re.compile(r'(?<!\d[hH])(?<=[A-Za-zÁÉÍÓÚáéíóú$%])([1-9]\d?)(?!\w|[.,]\d)')
_TABLE_LABEL = re.compile(r'(?im)^\s*Tabela\s+\d+\b')


def _with_explicit_context(blocks: list[SourceBlock], excerpts: list[SourceExcerpt]) -> list[SourceBlock]:
    """Resolve page-local explicit notes, then close required context finitely.

    A numbered footnote's subject is its explicit caller, never the nearest
    section. Missing/duplicate definitions remain unresolved and diagnosed.
    Positional table context remains available for unnumbered Nota/Fonte/Obs.
    """
    by_excerpt = {e.excerpt_id: e for e in excerpts}
    owners = {eid: index for index, block in enumerate(blocks) for eid in block.excerpt_ids}
    texts = ['\n'.join(by_excerpt[eid].text for eid in b.excerpt_ids) for b in blocks]
    contexts: list[list[str]] = [[] for _ in blocks]
    required: list[set[str]] = [set() for _ in blocks]
    diagnostics = [list(block.diagnostics) for block in blocks]
    # Store each definition separately: duplicate page/marker definitions must
    # not be combined into one apparent definition or resolved by first match.
    definitions: dict[tuple[int | None, str], list[list[int]]] = {}
    numbered: set[int] = set()
    positional_notes: set[int] = set()
    previous_definition: list[int] | None = None
    for index, block in enumerate(blocks):
        first = by_excerpt[block.excerpt_ids[0]]
        markers = list(_NOTE_DEFINITION.finditer(texts[index])) if block.kind == 'note' else []
        if markers:
            for marker in markers:
                previous_definition = [index]
                definitions.setdefault((first.ref.page, marker.group(1)), []).append(previous_definition)
            numbered.add(index)
            if texts[index][:markers[0].start()].strip():
                positional_notes.add(index)
                diagnostics[index].append('mixed_footnote_scope')
            if len(markers) > 1:
                diagnostics[index].append('multiple_footnotes_in_excerpt')
        elif (previous_definition and block.kind == 'note'
              and index == previous_definition[-1] + 1
              and 'long_block_split' in block.diagnostics
              and 'long_block_split' in blocks[index - 1].diagnostics
              and first.unit_id == by_excerpt[blocks[index - 1].excerpt_ids[-1]].unit_id
              and first.ref.start == by_excerpt[blocks[index - 1].excerpt_ids[-1]].ref.end):
            previous_definition.append(index)
            numbered.add(index)
        else:
            previous_definition = None

    referenced: set[int] = set()
    for index, block in enumerate(blocks):
        label_only = block.kind == 'table' and re.fullmatch(r'\s*Tabela\s+\d+\s*', texts[index], re.I)
        if index:
            contexts[index].extend(blocks[index - 1].excerpt_ids[-1:])
        if index + 1 < len(blocks):
            contexts[index].extend(blocks[index + 1].excerpt_ids[:1])
        # Numbered footnotes obtain their subject only from explicit references.
        if block.kind in {'table', 'note'} and (index not in numbered or index in positional_notes):
            for step in (-1, 1):
                adjacent = index + step
                while 0 <= adjacent < len(blocks) and blocks[adjacent].kind in {'table', 'note', 'section'}:
                    candidate = blocks[adjacent]
                    contexts[index].extend(candidate.excerpt_ids)
                    if candidate.kind == 'section':
                        if step == -1:
                            if not label_only or _SEMANTIC_HEADING.search(texts[adjacent]):
                                required[index].update(candidate.excerpt_ids)
                            # A projection table may have a separate label just
                            # before its title. Keep both as required context.
                            label = adjacent - 1
                            if label >= 0 and blocks[label].kind == 'table' and _TABLE_LABEL.search(texts[label]):
                                required[index].update(blocks[label].excerpt_ids)
                        elif (_SEMANTIC_HEADING.search(texts[adjacent])
                              and adjacent + 1 < len(blocks) and blocks[adjacent + 1].kind == 'table'):
                            required[index].update(candidate.excerpt_ids)
                        break
                    # Numbered notes have explicit scope; adjacency alone cannot
                    # make a table depend on an unrelated footnote.
                    if ((candidate.kind == 'note' and (adjacent not in numbered or adjacent in positional_notes))
                            or re.search(r'(?im)^\s*(?:Tabela\b|Variação.*\(%\)|Índice de preços)', texts[adjacent])):
                        required[index].update(candidate.excerpt_ids)
                    adjacent += step
            preceding = index - 1
            scenario: list[str] = []
            while preceding >= 0 and blocks[preceding].kind not in {'section', 'header', 'footer'}:
                if re.search(r'(?i)cenário de referência|hipóteses?|projeç(?:ão|ões)|condicionad[ao]', texts[preceding]):
                    scenario.extend(blocks[preceding].excerpt_ids)
                preceding -= 1
            if preceding >= 0 and blocks[preceding].kind == 'section':
                if not label_only or _SEMANTIC_HEADING.search(texts[preceding]):
                    required[index].update(blocks[preceding].excerpt_ids)
                label = preceding - 1
                if label >= 0 and blocks[label].kind == 'table' and _TABLE_LABEL.search(texts[label]):
                    required[index].update(blocks[label].excerpt_ids)
            required[index].update(scenario)

        # A note definition can mention another note, but its own marker at the
        # beginning is not a call (the regex requires a preceding letter/$/%).
        for eid in block.excerpt_ids:
            excerpt = by_excerpt[eid]
            for marker in _NOTE_REFERENCE.finditer(excerpt.text):
                key = (excerpt.ref.page, marker.group(1))
                candidates = definitions.get(key, [])
                word = re.search(r'(?<!\w)([A-Za-zÁÉÍÓÚáéíóú]+)$', excerpt.text[:marker.start()])
                token = re.search(r'\w+$', excerpt.text[:marker.start()])
                currency_amount = (excerpt.text[marker.start() - 1] == '$'
                    and not re.search(r'R\$\s*\d+(?:[.,]\d+)?/US\$$', excerpt.text[:marker.start()]))
                if (currency_amount or (token and any(char.isdigit() or char == '_' for char in token.group()))
                    or (word and (len(word.group(1)) == 1 or word.group(1).lower() in {'cm', 'mm', 'km', 'ml'}))):
                    diagnostics[index].append('ambiguous_footnote_reference')
                elif excerpt.ref.page is None:
                    diagnostics[index].append('footnote_page_unavailable')
                elif len(candidates) == 1:
                    definition = candidates[0]
                    for note in definition:
                        required[index].update(blocks[note].excerpt_ids)
                        required[note].update(block.excerpt_ids)
                        referenced.add(note)
                elif len(candidates) > 1:
                    diagnostics[index].append('ambiguous_footnote_definition')
                else:
                    elsewhere = any(number == key[1] for page, number in definitions if page != key[0])
                    diagnostics[index].append('footnote_cross_page_reference_unresolved' if elsewhere
                                              else 'unresolved_footnote_reference')
    for key, candidates in definitions.items():
        for definition in candidates:
            for note in definition:
                if key[0] is None:
                    diagnostics[note].append('footnote_page_unavailable')
                if len(candidates) > 1:
                    diagnostics[note].append('ambiguous_footnote_definition')
                if note not in referenced:
                    diagnostics[note].append('unreferenced_footnote_definition')

    # Graph traversal gives each caller and note the full mandatory table/
    # scenario/footnote context. A visited set makes reciprocal references and
    # cycles safe; traversal is bounded by the finite number of source blocks.
    closed: list[set[str]] = []
    for index, block in enumerate(blocks):
        visited = {index}
        pending = [index]
        closure: set[str] = set()
        while pending:
            current = pending.pop()
            closure.update(required[current])
            for eid in required[current]:
                owner = owners[eid]
                if owner not in visited:
                    visited.add(owner)
                    pending.append(owner)
        closure.difference_update(block.excerpt_ids)
        closed.append(closure)
    order = {excerpt.excerpt_id: index for index, excerpt in enumerate(excerpts)}
    return [block.model_copy(update={
        'context_ids': tuple(dict.fromkeys(eid for eid in (*contexts[index], *sorted(closed[index], key=order.__getitem__))
                                           if eid not in block.excerpt_ids)),
        'required_context_ids': tuple(sorted(closed[index], key=order.__getitem__)),
        'diagnostics': tuple(dict.fromkeys(diagnostics[index])),
    }) for index, block in enumerate(blocks)]


def _ranges(text: str, furniture: set[str] | None = None) -> list[tuple[int, int, str]]:
    """Recognize only explicit boundaries; uncertain layouts remain literal."""
    groups = []
    start = None
    kind = 'paragraph'
    for line in re.finditer(r'[^\n]*(?:\n|$)', text):
        if not line.group(): continue
        value = line.group().strip()
        new_kind = ('header' if furniture and value in furniture else 'footer' if re.fullmatch(r'\d+\s+(?:https?://)?[\w.-]+\.[a-z]{2,}(?:/.*)?',value) else 'table' if _TABLE.search(value) else 'note' if _NOTE.search(value)
                    else 'item' if _NUMBERED.match(value) else 'section' if _HEADING.match(value) else 'paragraph')
        # A wrapped footnote continues until an explicit structural boundary.
        # Treating its second line as narrative hides the unfinished item above
        # it when joining that item to the next physical page.
        if kind == 'note' and start is not None and value and new_kind == 'paragraph':
            new_kind = 'note'
        boundary = (not value or _NUMBERED.match(value) or new_kind == 'section'
                    or (new_kind != kind and (new_kind in {'table','note','header','footer'} or kind in {'table','note','section','header','footer'})))
        if start is not None and boundary:
            groups.append((start, line.start(), kind))
            start = None
        if value:
            if start is None:
                start, kind = line.start(), new_kind
    if start is not None: groups.append((start, len(text), kind))
    return [(a,b,k) for a,b,k in groups if text[a:b].strip()]


def _sentences(unit: ParsedUnit, start: int, end: int) -> list[tuple[int, int]]:
    # Decimal dots and footnote/abbreviation dots are never arbitrary cut points.
    boundaries = [start]
    for match in re.finditer(r'[.!?](?:[ \t]*\n|\s+(?=[A-ZÁÉÍÓÚ]))', unit.text[start:end]):
        boundaries.append(start + match.end())
    boundaries.append(end)
    return [(a,b) for a,b in zip(boundaries, boundaries[1:]) if unit.text[a:b].strip()]


def prepare_source(units: tuple[ParsedUnit, ...], counter: TokenCounter | None = None,
                   *, target_tokens: int = TARGET_SOURCE_TOKENS,
                   max_tokens: int = MAX_SOURCE_TOKENS) -> PreparedSource:
    if not units or not 1 <= target_tokens <= max_tokens <= MAX_SOURCE_TOKENS:
        raise ExpectedServiceError('invalid_source_plan', 'limites de segmentação inválidos')
    counter = counter or TokenCounter()
    from collections import Counter
    edge_lines = Counter(line.strip() for unit in units for line in unit.text.splitlines()[:4] if line.strip())
    furniture = {line for line,n in edge_lines.items() if n>1 and not _NUMBERED.match(line)
        and not _TABLE.search(line) and not _SEMANTIC_HEADING.search(line)}
    logical: list[tuple[str, list[tuple[ParsedUnit,int,int]], list[str]]] = []
    for unit in units:
        groups = _ranges(unit.text,furniture)
        for index,(start,end,kind) in enumerate(groups):
            diagnostics = ['ambiguous_table_layout'] if kind == 'table' else []
            logical.append((kind,[(unit,start,end)],diagnostics))
    # Skip identified running furniture and footnotes when connecting the first
    # narrative fragment to the unfinished narrative on the preceding page.
    connected = []
    for kind,spans,diagnostics in logical:
        unit,start,end = spans[0]
        previous = next((entry for entry in reversed(connected) if entry[0] in {'paragraph','item'}),None)
        intervening = connected[connected.index(previous)+1:] if previous else []
        continuation = (kind == 'paragraph' and previous and previous[1][-1][0].page is not None
            and unit.page == previous[1][-1][0].page+1
            and all(entry[0] in {'header','footer','note'} for entry in intervening)
            and unit.text[start:end].lstrip()[:1].islower()
            and not previous[1][-1][0].text[previous[1][-1][1]:previous[1][-1][2]].rstrip().endswith(('.', '!', '?')))
        if continuation:
            previous[1].extend(spans);previous[2].append('cross_page_continuation')
        else:
            connected.append((kind,spans,diagnostics))
    logical = connected
    pieces = []
    for kind, spans, diagnostics in logical:
        total = sum(counter.count(u.text[a:b]) for u,a,b in spans)
        if total <= target_tokens:
            pieces.append((kind,spans,diagnostics)); continue
        pending, size = [], 0
        for unit,start,end in spans:
            for a,b in _sentences(unit,start,end):
                tokens = counter.count(unit.text[a:b])
                if tokens > max_tokens:
                    raise ExpectedServiceError('source_unit_too_large', 'frase indivisível excede 2.000 tokens de fonte')
                if pending and size + tokens > target_tokens:
                    pieces.append((kind,pending,[*diagnostics,'long_block_split'])); pending,size = [],0
                pending.append((unit,a,b)); size += tokens
        if pending: pieces.append((kind,pending,[*diagnostics,'long_block_split']))
    excerpts, blocks = [], []
    for index,(kind,spans,diagnostics) in enumerate(pieces):
        ids = []
        for unit,start,end in spans:
            text = unit.text[start:end]
            identity = f'{unit.document_id}:{unit.parser_version}:{unit.unit_id}:{unit.start+start}:{unit.start+end}'
            eid = f'X{len(excerpts)+1:04}'
            cid = 'literal-' + sha256(identity.encode()).hexdigest()[:24]
            ref = EvidenceRef(document_id=unit.document_id, chunk_id=cid, parser_version=unit.parser_version,
                page=unit.page, start=unit.start+start,end=unit.start+end)
            excerpts.append(SourceExcerpt(excerpt_id=eid,unit_id=unit.unit_id,text=text,ref=ref)); ids.append(eid)
        blocks.append(SourceBlock(block_id=f'block-{index+1:04}',kind=kind,excerpt_ids=tuple(ids),diagnostics=tuple(diagnostics)))
    if not blocks: raise ExpectedServiceError('text_unavailable', 'fonte sem blocos utilizáveis')
    contextual = _with_explicit_context(blocks, excerpts)
    unit_order = {u.unit_id:i for i,u in enumerate(units)}
    excerpts.sort(key=lambda e:(unit_order[e.unit_id],e.ref.start))
    return PreparedSource(segmentation_version=SEGMENTATION_VERSION,target_source_tokens=target_tokens,max_source_tokens=max_tokens,document_id=units[0].document_id,
        units=units,excerpts=tuple(excerpts),blocks=tuple(contextual),
        diagnostics=('unrecognized_structure',) if all(b.kind=='paragraph' for b in blocks) else ())


def source_artifacts(source: PreparedSource) -> tuple[tuple[Chunk,...],tuple[Evidence,...]]:
    chunks = tuple(Chunk(**e.ref.model_dump(),unit_id=e.unit_id,text=e.text) for e in source.excerpts)
    evidence = tuple(Evidence(evidence_id=e.excerpt_id,ref=e.ref,text=e.text) for e in source.excerpts)
    return chunks,evidence
