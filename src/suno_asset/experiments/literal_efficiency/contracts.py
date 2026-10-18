"""Contratos compactos, cobertura lexical por ocorrência e seleção editorial.

Estes modelos são artefatos experimentais. O adaptador mantém o contrato
Anchor/ExtractionResult de produção sem trocar o texto completo dos suportes.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from suno_asset.domain.models import Anchor, AnchorKind, BlockProcessing, ExtractionResult, Importance, PreparedSource
from suno_asset.experiments.literal_efficiency import COVERAGE_VERSION, FACTS_VERSION, SELECTION_VERSION


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompactFact(Strict):
    id: str
    tipo: Literal["decision", "number", "date", "rationale", "risk", "outlook"]
    texto_literal: str = Field(min_length=1)
    apoio_ids: tuple[str, ...] = Field(min_length=1)
    owner_block_id: str
    page: int | None = None
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    provenance: Literal["llm", "rule"] = "llm"


class CompactFactPack(Strict):
    version: Literal["compact-facts-v1"] = FACTS_VERSION
    source_sha256: str
    segmentation_version: str
    facts: tuple[CompactFact, ...]
    verification: dict = Field(default_factory=dict)
    pending_material_checks: tuple[str, ...] = ()


class OccurrenceDisposition(Strict):
    occurrence_id: str
    kind: Literal["number", "percent", "date", "unit", "negation", "conditional_modal"]
    excerpt_id: str
    page: int | None
    start: int
    end: int
    text: str
    disposition: Literal["covered", "required_context", "ignored", "pending_review", "uncovered"]
    fact_ids: tuple[str, ...] = ()
    reason: str | None = None


class OccurrenceCoverage(Strict):
    version: Literal["occurrence-coverage-v1"] = COVERAGE_VERSION
    occurrences: tuple[OccurrenceDisposition, ...]
    alerts: tuple[str, ...] = ()
    semantic_recall: Literal["not_measured_here"] = "not_measured_here"


class SelectionFact(Strict):
    fact_id: str
    reason: str
    role: Literal["decision", "guidance", "rationale", "risk", "projection", "context", "other"]
    mandatory: bool = False
    dependency_ids: tuple[str, ...] = ()
    estimated_words: int = Field(ge=0)


class EditorialSelection(Strict):
    version: Literal["selection-v1"] = SELECTION_VERSION
    factpack_sha256: str
    audience: str
    output_format: str
    capacity: dict
    capacity_estimate: Literal["conservative_estimate"] = "conservative_estimate"
    facts: tuple[SelectionFact, ...]
    status: Literal["suggested", "confirmed", "pending"] = "suggested"
    alerts: tuple[str, ...] = ()


_EXPLICIT_DECISION = re.compile(
    r"(?is)\b(?:o\s+)?(?:copom|comit[eê])\s+decidiu\s+(?:reduzir|elevar|manter|fixar|alterar)\b.{0,180}\b(?:selic|taxa\s+b[aá]sica)\b")
_EXPLICIT_GUIDANCE = re.compile(
    r"(?is)(?:\bmagnitude do ciclo de calibra[cç][aã]o ser[aá] ajustada\b|"
    r"\b(?:o comit[eê]|o copom) seguir[aá] incorporando\b.{0,180}\bmanter a restri[cç][aã]o adequada\b|"
    r"\b(?:o comit[eê]|o copom) continuar[aá] acompanhando\b.{0,180}\bcalibrar e refinar\b)")


def is_explicit_decision(fact: CompactFact) -> bool:
    return fact.tipo == 'decision' and bool(_EXPLICIT_DECISION.search(fact.texto_literal))


def is_explicit_guidance(fact: CompactFact) -> bool:
    # Extracted literals can preserve PDF line breaks between ordinary words.
    # Normalize whitespace only for recognition; keep the stored source text exact.
    normalized = " ".join(fact.texto_literal.split())
    return bool(_EXPLICIT_GUIDANCE.search(normalized))


FACT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "facts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"block_id":{"type":"string"},
                "tipo": {"type": "string", "enum": ["decision", "number", "date", "rationale", "risk", "outlook"]},
                "texto_literal": {"type": "string"}, "apoio_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["block_id","tipo", "texto_literal", "apoio_ids"]}}
    }, "required": ["facts"]
}

_OCCURRENCES = (
    ("percent", re.compile(r"(?<!\w)\d+(?:[,.]\d+)?\s*%")),
    ("date", re.compile(r"(?<!\w)(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}|\d{1,2}º? trimestre(?: de \d{4})?)", re.I)),
    ("number", re.compile(r"(?<![\w.,])\d+(?:[,.]\d+)?(?!\w|[.,]\d)")),
    ("unit", re.compile(r"(?i)(?:pontos? percentuais?|p\.p\.|bilh(?:ão|ões)|milh(?:ão|ões)|US\$|R\$|dólares?|reais)")),
    ("negation", re.compile(r"(?i)\b(?:não|nem|nunca|sem|tampouco)\b")),
    ("conditional_modal", re.compile(r"(?i)\b(?:se|caso|condicionado|condicionada|poderá|poderiam|deverá|espera-se|projeta-se|tende a)\b")),
)


def _stable_id(source_sha: str, block_id: str, start: int, end: int, kind: str, text: str) -> str:
    digest = sha256(f"{source_sha}|{block_id}|{start}|{end}|{kind}|{text}".encode()).hexdigest()[:16]
    return "F" + digest


def normalize_model_facts(source: PreparedSource, block_id: str, rows: list[dict], *, source_sha256: str,
                          provenance: str = "llm") -> tuple[CompactFact, ...]:
    """Validate support scope and locate the exact fact string in one source excerpt."""
    block = next((item for item in source.blocks if item.block_id == block_id), None)
    if block is None:
        raise ValueError("bloco desconhecido")
    by_id = {item.excerpt_id: item for item in source.excerpts}
    allowed = set((*block.excerpt_ids, *block.required_context_ids))
    owned = set(block.excerpt_ids)
    found: list[CompactFact] = []
    seen: set[tuple] = set()
    for row in rows:
        text = row["texto_literal"]
        supports = tuple(row["apoio_ids"])
        if (not supports or len(supports) != len(set(supports)) or set(supports) - allowed
            or not set(supports) & owned):
            raise ValueError("apoio inexistente, duplicado ou fora do escopo")
        # Models commonly normalize PDF line wraps to spaces. Resolve that
        # whitespace-only difference against the owned source text, then store
        # the exact source slice and its trusted offsets. Context-only matches
        # cannot create a fact, and repeated matches in the owned block remain
        # explicitly ambiguous.
        candidates = []
        whitespace_pattern = re.compile(r"\s+".join(re.escape(part) for part in text.split()))
        for excerpt_id in supports:
            excerpt = by_id[excerpt_id]
            if excerpt_id not in owned:
                continue
            candidates.extend((excerpt, match.start(), match.end())
                for match in whitespace_pattern.finditer(excerpt.text))
        if len(candidates) != 1:
            raise ValueError("literal ausente ou localização ambígua; exige correção")
        excerpt, local_start, local_end = candidates[0]
        text = excerpt.text[local_start:local_end]
        start, end = excerpt.ref.start + local_start, excerpt.ref.start + local_end
        key = (excerpt.excerpt_id, start, end, row["tipo"], tuple(sorted(supports)))
        if key in seen:
            continue
        seen.add(key)
        found.append(CompactFact(id=_stable_id(source_sha256, block_id, start, end, row["tipo"], text),
            tipo=row["tipo"], texto_literal=text, apoio_ids=supports, owner_block_id=block_id,
            page=excerpt.ref.page, start=start, end=end, provenance=provenance))
    return tuple(found)


def adapt_to_legacy(source: PreparedSource, pack: CompactFactPack) -> ExtractionResult:
    """Rebuild full literal supports; only safe, explicit legacy metadata is used."""
    by_id = {item.excerpt_id: item for item in source.excerpts}
    grouped: dict[str, list[CompactFact]] = {}
    for fact in pack.facts:
        grouped.setdefault(fact.owner_block_id, []).append(fact)
    anchors, processing = [], []
    for block in source.blocks:
        facts = grouped.get(block.block_id, [])
        assigned = []
        for fact in facts:
            support_ids = set((*fact.apoio_ids, *block.required_context_ids))
            support = tuple(excerpt for excerpt in source.excerpts if excerpt.excerpt_id in support_ids)
            explicit_decision=is_explicit_decision(fact)
            explicit_guidance=is_explicit_guidance(fact)
            kind = AnchorKind("number" if fact.tipo == "date" else
                "decision" if explicit_decision else "outlook" if fact.tipo == "decision" or explicit_guidance else fact.tipo)
            importance = Importance.CRITICAL if explicit_decision or explicit_guidance else Importance.SUPPORT
            identity = sha256(json.dumps([fact.id, sorted(support_ids)], separators=(",", ":")).encode()).hexdigest()[:12]
            anchor_id = "A" + identity
            anchors.append(Anchor(anchor_id=anchor_id, kind=kind, importance=importance,
                statement="\n".join(excerpt.text for excerpt in support),
                evidence_refs=tuple(excerpt.ref for excerpt in support), support_excerpts=support,
                numeric_facts=(), owner_block_id=block.block_id, extraction_version="extraction-v1"))
            assigned.append(anchor_id)
        processing.append(BlockProcessing(block_id=block.block_id,
            classification="material" if assigned else "no_relevant_facts",
            reason="compact-facts-adapter", anchor_ids=tuple(assigned), batch_id="compact-facts-v1"))
    return ExtractionResult(anchors=tuple(anchors), processing=tuple(processing))


def occurrence_coverage(source: PreparedSource, facts: tuple[CompactFact, ...], *,
                        ignored: dict[str, str] | None = None) -> OccurrenceCoverage:
    ignored = ignored or {}
    rows = []
    by_id = {excerpt.excerpt_id: excerpt for excerpt in source.excerpts}
    for excerpt in source.excerpts:
        for kind, pattern in _OCCURRENCES:
            for index, match in enumerate(pattern.finditer(excerpt.text)):
                start, end = excerpt.ref.start + match.start(), excerpt.ref.start + match.end()
                covering = tuple(fact.id for fact in facts if excerpt.excerpt_id in fact.apoio_ids
                    and fact.start <= start and fact.end >= end)
                key = f"{excerpt.excerpt_id}:{kind}:{start}:{end}"
                # Only structurally proven page/item/note markers are auto-ignored.
                line_start = excerpt.text.rfind("\n", 0, match.start()) + 1
                prefix = excerpt.text[line_start:match.start()]
                suffix = excerpt.text[match.end():]
                structural = kind == "number" and not prefix.strip() and re.match(r"[.)]\s", suffix)
                if key in ignored:
                    disposition, reason, fact_ids = "pending_review", ignored[key], ()
                elif structural:
                    disposition, reason, fact_ids = "ignored", "marcador estrutural de item", ()
                elif covering:
                    disposition, reason, fact_ids = "covered", None, covering
                elif any(excerpt.excerpt_id in f.apoio_ids for f in facts):
                    disposition, reason, fact_ids = "required_context", "ocorrência no suporte citado, sem cobertura semântica atribuída", ()
                else:
                    disposition, reason, fact_ids = "uncovered", None, ()
                rows.append(OccurrenceDisposition(occurrence_id=f"{excerpt.excerpt_id}:{kind}:{index}:{start}",
                    kind=kind, excerpt_id=excerpt.excerpt_id, page=excerpt.ref.page, start=start, end=end,
                    text=match.group(), disposition=disposition, fact_ids=fact_ids, reason=reason))
    alerts = tuple(f"{r.occurrence_id}: {r.text!r} exige revisão" for r in rows
        if r.disposition in {"uncovered","pending_review"})
    return OccurrenceCoverage(occurrences=tuple(rows), alerts=alerts)


def selection_suggestion(pack: CompactFactPack, audience: str, output_format: str,
                         roles: dict[str, dict] | None = None) -> EditorialSelection:
    roles = roles or {}
    fact_ids = {fact.id for fact in pack.facts}
    decisions = [f for f in pack.facts if f.tipo == "decision"]
    guidance = [f for f in pack.facts if f.tipo == "outlook" and re.search(
        r"(?i)\b(?:orienta|guidance|comunica|manter|manterá|permanecer|decid|futuras? reuniões?)", f.texto_literal)]
    core = {f.id for f in (*decisions, *guidance)}
    ordered_roles, alerts = [], []
    for fact in pack.facts:
        row = roles.get(fact.id, {})
        role = row.get("role") or ("decision" if fact.tipo == "decision" else
            "guidance" if fact in guidance else "risk" if fact.tipo == "risk" else
            "projection" if fact.tipo == "outlook" else "rationale" if fact.tipo == "rationale" else "other")
        deps = tuple(row.get("dependency_ids", ()))
        if set(deps) - fact_ids:
            raise ValueError("dependência editorial aponta para fato inexistente")
        mandatory = fact.id in core or role == "guidance" or bool(row.get("mandatory", False))
        if row.get("uncertain"):
            alerts.append(f"{fact.id}: classificação editorial incerta; revisar núcleo e dependências")
        words = max(1, (len(fact.texto_literal.split()) * 3 + 1) // 2 + 5)
        ordered_roles.append(SelectionFact(fact_id=fact.id,
            reason="núcleo obrigatório disponível" if fact.id in core else row.get("reason", "relevância sugerida; revisar"),
            role=role, mandatory=mandatory, dependency_ids=deps, estimated_words=words))
    rank = {"decision": 0, "guidance": 1, "rationale": 2, "risk": 3, "projection": 4, "context": 5, "other": 6}
    ordered_roles.sort(key=lambda item: (not item.mandatory, rank[item.role],
        next(i for i, f in enumerate(pack.facts) if f.id == item.fact_id)))
    if output_format == "informational_carousel":
        capacity = {"cards": 6, "reserved_cards": 2, "available_fact_cards": 4, "estimated_words": 240}
        max_items = 4
    elif output_format == "short_video_script":
        capacity = {"seconds": 60, "spoken_words": 130, "reserved_words": 30}
        max_items = None
    else:
        capacity = {"words": 800, "reserved_words": 80}
        max_items = None
    budget = capacity.get("estimated_words", capacity.get("spoken_words", capacity.get("words", 800)))
    selected, spent = [], 0
    for row in ordered_roles:
        next_cost = row.estimated_words + sum(
            next((x.estimated_words for x in ordered_roles if x.fact_id == dep), 0) for dep in row.dependency_ids)
        if row.mandatory or (spent + next_cost <= budget and (max_items is None or len(selected) < max_items)):
            selected.append(row)
            spent += next_cost
        else:
            alerts.append(f"{row.fact_id}: fora da sugestão por capacidade estimada; seleção editável")
    selected_ids = {row.fact_id for row in selected}
    for row in selected:
        missing = set(row.dependency_ids) - selected_ids
        if missing:
            alerts.append(f"{row.fact_id}: dependências não selecionadas {sorted(missing)}")
    if decisions and not any(row.fact_id in core for row in selected):
        alerts.append("núcleo decisório não confirmado")
    if spent>budget or (max_items is not None and len(selected)>max_items):
        alerts.append("seleção excede capacidade estimada; revise formato ou extensão")
    if any("dependências não selecionadas" in alert for alert in alerts):
        alerts.append("dependências editoriais pendentes")
    return EditorialSelection(factpack_sha256=sha256(pack.model_dump_json().encode()).hexdigest(),
        audience=audience, output_format=output_format, capacity=capacity, facts=tuple(selected),
        status="pending" if any("núcleo" in a or "dependências editoriais pendentes" in a or "incerta" in a for a in alerts) else "suggested", alerts=tuple(alerts))


def validate_selection(selection: EditorialSelection, pack: CompactFactPack, *, confirm: bool = False) -> EditorialSelection:
    expected_hash = sha256(pack.model_dump_json().encode()).hexdigest()
    if selection.factpack_sha256 != expected_hash:
        raise ValueError("selection aponta para outra versão de ficha")
    ids = {fact.id for fact in pack.facts}
    selected = {row.fact_id for row in selection.facts}
    if selected - ids:
        raise ValueError("selection contém IDs obsoletos")
    decisions = {fact.id for fact in pack.facts if fact.tipo == "decision"}
    guidance = {fact.id for fact in pack.facts if fact.tipo == "outlook" and re.search(
        r"(?i)\b(?:orienta|guidance|comunica|manter|manterá|permanecer|decid|futuras? reuniões?)", fact.texto_literal)}
    mandatory = decisions | guidance
    for row in selection.facts:
        if row.fact_id in mandatory and not row.mandatory:
            raise ValueError(f"núcleo obrigatório marcado como opcional: {row.fact_id}")
        if set(row.dependency_ids) - selected:
            raise ValueError(f"dependência removida para {row.fact_id}")
        if set(row.dependency_ids) - ids:
            raise ValueError(f"dependência aponta para fato inexistente: {row.fact_id}")
    if mandatory - selected:
        raise ValueError(f"selection removeu núcleo obrigatório: {sorted(mandatory-selected)}")
    # A caller cannot edit away a dependency by deleting the row that declared it.
    # The authoritative suggested selection is reconstructed from the pack.
    authoritative = {row.fact_id: row for row in selection_suggestion(
        pack, selection.audience, selection.output_format).facts}
    for fact_id in mandatory:
        if fact_id in authoritative:
            deps = set(authoritative[fact_id].dependency_ids)
            if deps - selected:
                raise ValueError(f"dependências obrigatórias removidas para {fact_id}: {sorted(deps-selected)}")
    return selection.model_copy(update={"status": "confirmed" if confirm else selection.status})
