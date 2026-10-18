"""Chunking e recuperação lexical determinísticos para o vertical slice simulado."""

from __future__ import annotations

from hashlib import sha256
import re
import unicodedata

from suno_asset.domain.models import (
    Anchor, AnchorKind, Chunk, DocumentMetadata, Evidence, EvidenceRef, Importance, ParsedUnit,
)

CHUNK_SIZE = 1800
CHUNK_OVERLAP = 280
RETRIEVAL_VERSION = "lexical-v1"
FACETS: dict[AnchorKind, tuple[str, ...]] = {
    AnchorKind.DECISION: ("copom", "decidiu", "decisão", "selic", "taxa"),
    AnchorKind.NUMBER: ("%", "projeção", "expectativa", "ipca", "2026", "2027"),
    AnchorKind.RATIONALE: ("porque", "devido", "demanda", "atividade", "política", "inflação"),
    AnchorKind.RISK: ("risco", "incerteza", "alta", "baixa", "condicional"),
    AnchorKind.OUTLOOK: ("perspectiva", "cenário", "futuro", "horizonte", "convergência"),
}


def _fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c))


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _fold(value)))


def chunk_units(document: DocumentMetadata, units: tuple[ParsedUnit, ...]) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for unit in units:
        text = unit.text
        start = 0
        while start < len(text):
            end = min(start + CHUNK_SIZE, len(text))
            if end < len(text):
                # Preferir fim de parágrafo/linha sem produzir fragmentos minúsculos.
                cuts = [text.rfind(marker, start + CHUNK_SIZE // 2, end) for marker in ("\n\n", "\n", ". ")]
                cut = max(cuts)
                if cut > start + CHUNK_SIZE // 2:
                    end = cut + (2 if text[cut:cut + 2] in ("\n\n", ". ") else 1)
            if end <= start:
                end = min(start + CHUNK_SIZE, len(text))
            absolute_start, absolute_end = unit.start + start, unit.start + end
            digest = sha256(
                f"{document.sha256}|{unit.parser_version}|{RETRIEVAL_VERSION}|{CHUNK_SIZE}|{CHUNK_OVERLAP}|{unit.unit_id}|{absolute_start}|{absolute_end}".encode()
            ).hexdigest()[:24]
            chunks.append(Chunk(
                chunk_id=f"chunk-{digest}", document_id=document.document_id,
                unit_id=unit.unit_id, parser_version=unit.parser_version, page=unit.page,
                start=absolute_start, end=absolute_end, text=text[start:end],
            ))
            if end == len(text):
                break
            start = max(start + 1, end - CHUNK_OVERLAP)
    return tuple(chunks)


class LexicalRetriever:
    """Recupera evidências por faceta; foco só acrescenta candidatos."""

    def __init__(self) -> None:
        self.last_missing_facets: tuple[str, ...] = ()

    def retrieve(
        self, document: DocumentMetadata, units: tuple[ParsedUnit, ...], focus: str | None
    ) -> tuple[tuple[Chunk, ...], tuple[Evidence, ...]]:
        chunks = chunk_units(document, units)
        chunk_tokens = tuple(_tokens(chunk.text) for chunk in chunks)
        index: dict[str, set[int]] = {}
        for position, tokens in enumerate(chunk_tokens):
            for token in tokens:
                index.setdefault(token, set()).add(position)
        chosen: set[str] = set()
        scores: dict[str, int] = {}
        missing: list[str] = []
        for facet, terms in FACETS.items():
            term_tokens = set().union(*(_tokens(term) for term in terms))
            candidates = set().union(*(index.get(token, set()) for token in term_tokens))
            if facet == AnchorKind.NUMBER:
                candidates.update(i for i, chunk in enumerate(chunks)
                                  if re.search(r"\d+(?:[.,]\d+)?\s*%", chunk.text))
            scored = []
            for position in candidates:
                chunk = chunks[position]
                overlap = len(chunk_tokens[position] & term_tokens)
                if facet == AnchorKind.NUMBER and re.search(r"\d+(?:[.,]\d+)?\s*%", chunk.text):
                    overlap += 2
                if overlap:
                    scored.append((overlap, chunk))
                    scores[chunk.chunk_id] = max(scores.get(chunk.chunk_id, 0), overlap)
            if not scored:
                missing.append(facet.value)
            else:
                scored.sort(key=lambda pair: (-pair[0], pair[1].page or 0, pair[1].start))
                chosen.update(chunk.chunk_id for _, chunk in scored[:3])
        if focus:
            terms = _tokens(focus)
            candidates = set().union(*(index.get(token, set()) for token in terms))
            scored = [(len(chunk_tokens[i] & terms), chunks[i]) for i in candidates]
            scored.sort(key=lambda pair: (-pair[0], pair[1].page or 0, pair[1].start))
            chosen.update(chunk.chunk_id for score, chunk in scored[:4] if score)
            # Score persistido é independente do foco para manter a evidência imutável.
        # Preservar contexto adjacente, inclusive a continuação na página seguinte.
        selected_indices = {i for i, chunk in enumerate(chunks) if chunk.chunk_id in chosen}
        selected_indices |= {j for i in tuple(selected_indices) for j in (i - 1, i + 1) if 0 <= j < len(chunks)}
        selected = tuple(chunk for i, chunk in enumerate(chunks) if i in selected_indices)
        self.last_missing_facets = tuple(missing)
        evidence = tuple(Evidence(
            evidence_id=f"evidence-{chunk.chunk_id}",
            ref=EvidenceRef(
                document_id=chunk.document_id, chunk_id=chunk.chunk_id,
                parser_version=chunk.parser_version, page=chunk.page,
                start=chunk.start, end=chunk.end,
            ), text=chunk.text, retrieval_score=scores.get(chunk.chunk_id, 0),
        ) for chunk in selected)
        return chunks, evidence


class FullSourceRetriever(LexicalRetriever):
    """Real evaluation accounts for every parsed chunk, including low-score facts."""
    def retrieve(self, document, units, focus):
        chunks, selected = super().retrieve(document, units, focus)
        by_ref = {item.ref: item for item in selected}
        evidence = []
        for chunk in chunks:
            ref = EvidenceRef(document_id=chunk.document_id, chunk_id=chunk.chunk_id,
                parser_version=chunk.parser_version, page=chunk.page, start=chunk.start, end=chunk.end)
            evidence.append(by_ref.get(ref) or Evidence(evidence_id=f'evidence-{chunk.chunk_id}',
                ref=ref, text=chunk.text))
        return chunks, tuple(evidence)


class DeterministicExtractor:
    """Uma âncora literal por evidência financeira recuperada; não usa rótulos humanos."""

    def extract(self, evidence: tuple[Evidence, ...]) -> tuple[Anchor, ...]:
        anchors: list[Anchor] = []
        seen: set[str] = set()
        for item in evidence:
            if item.evidence_id in seen:
                continue
            seen.add(item.evidence_id)
            text = _tokens(item.text)
            numbers = bool(re.search(r"\d+(?:[.,]\d+)?\s*%", item.text))
            matching = [(len(text & set().union(*(_tokens(term) for term in terms))), kind)
                        for kind, terms in FACETS.items()]
            score, kind = max(matching, key=lambda pair: pair[0])
            if score == 0 and not numbers:
                continue
            if numbers and score == 0:
                kind = AnchorKind.NUMBER
            critical = numbers or bool(text & {'decidiu', 'decisao', 'selic', 'riscos', 'risco', 'projecoes', 'expectativas'})
            anchors.append(Anchor(
                anchor_id=f"anchor-{item.evidence_id}", kind=kind,
                importance=Importance.CRITICAL if critical else Importance.SUPPORT,
                statement=item.text, evidence_refs=(item.ref,),
            ))
        return tuple(anchors)
