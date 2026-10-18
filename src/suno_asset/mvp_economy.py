"""MVP policy helpers for the compact, cached literal pipeline."""
from __future__ import annotations

from hashlib import sha256
import json
import re

from suno_asset.domain.models import Anchor, EvaluationPolicy
from suno_asset.experiments.literal_efficiency.contracts import (CompactFactPack,
    is_explicit_decision,is_explicit_guidance)

MVP_POLICY = EvaluationPolicy(version="mvp-efficient-v1",
    required_checks=("structure", "coverage", "support", "numbers", "citations", "adequacy"))


def economy_policy(provider='groq'):
    if provider == 'gemini':
        from suno_asset.services.hybrid_facts import POLICY
        return POLICY
    return MVP_POLICY

GUIDANCE = re.compile(r"(?i)\b(?:orienta|guidance|comunica|manter|manterá|permanecer|futuras? reuniões?|decisões futuras)\b")


def select_anchors(pack: CompactFactPack, anchors: tuple[Anchor, ...], output_format: str,
                   source=None) -> tuple[tuple[Anchor, ...], dict]:
    """Keep decision/explicit guidance, then add the strongest compact facts by format."""
    facts = {fact.id: fact for fact in pack.facts}
    anchor_for_fact = {}
    context_by_block = ({block.block_id:set(block.required_context_ids) for block in source.blocks}
        if source is not None else {})
    for fact in pack.facts:
        support_ids = set(fact.apoio_ids) | context_by_block.get(fact.owner_block_id,set())
        identity = sha256(json.dumps([fact.id, sorted(support_ids)], separators=(",", ":")).encode()).hexdigest()[:12]
        anchor_for_fact[fact.id] = "A" + identity
    anchors_by_id = {anchor.anchor_id: anchor for anchor in anchors}
    if any(anchor_id not in anchors_by_id for anchor_id in anchor_for_fact.values()):
        raise ValueError("compact factpack e âncoras adaptadas divergentes")
    core = {fact.id for fact in pack.facts if is_explicit_decision(fact) or is_explicit_guidance(fact)}
    if output_format == "analytical_text":
        budget, max_facts = 500, 6
    elif output_format == "informational_carousel":
        budget, max_facts = 240, 4
    else:
        budget, max_facts = 90, None
    def effective_rank(fact):
        if is_explicit_decision(fact):return 0
        if is_explicit_guidance(fact):return 1
        return {"rationale":2,"risk":3,"outlook":4,"number":4,"date":5}.get(fact.tipo,6)
    ordered = sorted(pack.facts, key=lambda fact: (fact.id not in core, effective_rank(fact), fact.start))
    chosen, words = [], 0
    def add_fact(fact):
        nonlocal words
        if fact.id in chosen:
            return
        amount = max(1, len(fact.texto_literal.split()))
        mandatory = fact.id in core
        if not mandatory and (words + amount > budget or (max_facts is not None and len(chosen) >= max_facts)):
            return
        chosen.append(fact.id)
        words += amount
    for fact in ordered:
        if fact.id in core:
            add_fact(fact)
    # Give the first available item from each useful category a chance before
    # filling remaining capacity with a second fact of the same kind.
    for kind in ("rationale", "risk", "outlook", "number", "date"):
        candidate = next((fact for fact in ordered if fact.id not in core and fact.tipo == kind), None)
        if candidate is not None:
            add_fact(candidate)
    for fact in ordered:
        add_fact(fact)
    if not core:
        raise ValueError("núcleo editorial incerto: decisão/guidance não identificável")
    selected_anchors = tuple(anchors_by_id[anchor_for_fact[fact_id]] for fact_id in chosen)
    manifest = {"version":"mvp-selection-v3", "factpack_sha256":sha256(pack.model_dump_json().encode()).hexdigest(),
        "output_format":output_format, "capacity":{"max_words":budget,"max_fact_cards":max_facts},
        "estimated_fact_words":words, "selected_fact_ids":chosen,
        "omitted_fact_ids":[fact.id for fact in pack.facts if fact.id not in set(chosen)],
        "mandatory_fact_ids":sorted(core), "coverage_scope":"selected_facts_only",
        "status":"confirmed", "alerts":["núcleo excede capacidade estimada"] if words > budget else []}
    return selected_anchors, manifest
