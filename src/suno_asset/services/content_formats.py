"""One requested presentation, preserving the exact evaluated claim text."""
from __future__ import annotations

import re
from pydantic import TypeAdapter

from suno_asset.domain.models import (
    Audience, CarouselCard, CarouselContent, Content, ContentBlock,
    EvaluationIssue, GeneratedContent, OutputFormat, ShortVideoContent, SpokenBlock, VisualSupport,
)
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.glossary import GLOSSARY

AUDIENCE_RUBRICS = {
    'beginner': 'Explique termos essenciais, evite jargões sem definição e use frases claras.',
    'intermediate': 'Evite explicações básicas repetidas; explique conceitos menos comuns.',
    'advanced': 'Permita maior densidade técnica e apenas relações explicitamente sustentadas pela fonte.',
}
FACTUAL_RUBRIC = 'Nenhum público autoriza causalidade inventada, recomendações ou perda de condições e negações.'


def validate_content(value):
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='python')
    return TypeAdapter(Content).validate_python(value)


def _display(block):
    return ' '.join((block.text, *block.educational_notes))


def presentation_sections(content) -> dict[str, str]:
    if isinstance(content, GeneratedContent):
        return {name: getattr(content, name) for name in ('synthesis', 'development', 'implications')}
    if isinstance(content, CarouselContent):
        return {'hook': _display(content.hook), **{f'card-{card.order}': _display(card) for card in content.cards},
                'conclusion': _display(content.conclusion)}
    return {'hook': _display(content.hook),
            **{f'spoken-{i}': _display(block) for i, block in enumerate(content.spoken_blocks, 1)},
            **{f'visual-{i}': _display(block) for i, block in enumerate(content.visual_support, 1)}}


def format_errors(content, *, economy: bool = False) -> tuple[EvaluationIssue, ...]:
    reason = None
    if not isinstance(content, GeneratedContent):
        allowed = {f'Contexto educacional — {entry["term"]}: {entry["definition"]}' for entry in GLOSSARY}
        if any(note not in allowed for block in content.display_blocks for note in block.educational_notes):
            return (EvaluationIssue(code='invalid_education', message='Definição educacional fora do glossário versionado.', correctable=False, hard_failure=True),)
    if isinstance(content, CarouselContent):
        if economy and len(content.cards) > 4:
            reason = 'Perfil economy limita o carrossel a seis telas: abertura, até quatro cards e conclusão.'
        elif not economy and not 5 <= len(content.cards) <= 10:
            reason = 'Carrossel exige entre 5 e 10 cards de conteúdo.'
    if economy and isinstance(content, GeneratedContent):
        words = len(' '.join((content.synthesis, content.development, content.implications)).split())
        if words > 500:
            reason = f'Texto analítico excede 500 palavras ({words}).'
    if isinstance(content, ShortVideoContent) and content.computed_duration_seconds > 60:
        reason = 'Roteiro supera 60 segundos a 130 palavras por minuto, incluindo pausas.'
    return (() if reason is None else (EvaluationIssue(code='format_noncompliant', message=reason,
        correctable=True, hard_failure=False),))


def _split_claims(claims):
    """Split full simulated excerpts, retaining support/anchor links and every word."""
    result = []
    for claim in claims:
        parts = [part.strip() for part in re.split(r'(?<=[.!?])(?<!\d\.)\s+(?=[A-ZÀ-Ú])', claim.text) if part.strip()]
        if not parts:
            parts = [claim.text]
        result.extend(claim.model_copy(update={'claim_id': f'{claim.claim_id}-part-{i}', 'text': part})
                      for i, part in enumerate(parts, 1))
    return tuple(result)


def to_format(content: GeneratedContent, output_format: OutputFormat | str):
    output_format = OutputFormat(output_format)
    if output_format == OutputFormat.ANALYTICAL_TEXT:
        return content
    claims = _split_claims(content.claims)
    base = dict(content_id=content.content_id, revision=content.revision, audience=content.audience,
                title=content.title, claims=claims)
    definitions = {entry['term']: f'Contexto educacional — {entry["term"]}: {entry["definition"]}' for entry in GLOSSARY}
    emitted = set()
    def block(claim, *, educational=True):
        notes = []
        if educational and content.audience == Audience.BEGINNER:
            for term, definition in definitions.items():
                if term not in emitted and re.search(r'\b' + re.escape(term) + r'\b', claim.text, re.I):
                    emitted.add(term)
                    notes.append(definition)
        return dict(text=claim.text, claim_ids=(claim.claim_id,), educational_notes=tuple(notes))
    if output_format == OutputFormat.INFORMATIONAL_CAROUSEL:
        # A repeated hook/conclusion is presentation, never a new factual assertion.
        result = CarouselContent(**base, hook=ContentBlock(**block(claims[0], educational=False)),
            cards=tuple(CarouselCard(order=i, **block(claim)) for i, claim in enumerate(claims, 1)),
            conclusion=ContentBlock(**block(claims[-1], educational=False)))
    else:
        # Hook is also spoken; do not count it twice by repeating it in the body.
        spoken = claims[1:] or claims
        hook = ContentBlock(**block(claims[0]))
        spoken_blocks = tuple(SpokenBlock(**block(c)) for c in spoken)
        words = len(_display(hook).split()) + sum(len(_display(b).split()) for b in spoken_blocks)
        result = ShortVideoContent(**base, hook=hook, spoken_blocks=spoken_blocks,
            visual_support=tuple(VisualSupport(**block(c, educational=False)) for c in claims),
            estimated_duration_seconds=words * 60 / 130)
    return result



def structured_from_sections(*, content_id, revision, audience, output_format, sections, claims, claim_positions, pause_positions=None):
    """Consolidate the requested writer schema without dropping hook/visual claims."""
    pause_positions = pause_positions or {}
    definitions = {f'Contexto educacional — {entry["term"]}: {entry["definition"]}' for entry in GLOSSARY}
    educational = []
    rows = {}
    for name, values in sections.items():
        rows[name] = []
        for value in values:
            position = claim_positions.get((name, value))
            if position is None:
                if value not in definitions:
                    raise ExpectedServiceError('invalid_generation', 'Bloco sem afirmação ou definição reconhecida.')
                educational.append(value)
                continue
            rows[name].append(ContentBlock(text=value, claim_ids=(claims[position].claim_id,)))
    def combine(values, fallback):
        values = values or [fallback]
        return ContentBlock(text=' '.join(block.text for block in values),
            claim_ids=tuple(cid for block in values for cid in block.claim_ids))
    base = dict(content_id=content_id, revision=revision, audience=audience,
        title='Análise da ata do Copom', claims=tuple(claims))
    if str(output_format) == 'informational_carousel':
        cards = rows['cards']
        if not cards:
            raise ExpectedServiceError('invalid_generation', 'Carrossel sem cards materiais.')
        hook = combine(rows['hook'], cards[0])
        conclusion = combine(rows['conclusion'], cards[-1])
        hook = hook.model_copy(update={'educational_notes':tuple(dict.fromkeys(educational))})
        return CarouselContent(**base, hook=hook,
            cards=tuple(CarouselCard(order=i, **card.model_dump()) for i, card in enumerate(cards, 1)),
            conclusion=conclusion)
    spoken = rows['spoken_blocks']
    if not spoken:
        raise ExpectedServiceError('invalid_generation', 'Roteiro sem blocos falados.')
    hook = combine(rows['hook'], spoken[0])
    hook = hook.model_copy(update={'educational_notes':tuple(dict.fromkeys(educational))})
    visual = rows['visual_support'] or [spoken[0]]
    words = len(_display(hook).split()) + sum(len(_display(block).split()) for block in spoken)
    spoken_blocks = tuple(SpokenBlock(**block.model_dump(),pause_seconds=pause_positions.get(('spoken_blocks',block.text),0)) for block in spoken)
    moved_pauses = sum(pause_positions.values()) - sum(block.pause_seconds for block in spoken_blocks)
    if moved_pauses:
        spoken_blocks = (spoken_blocks[0].model_copy(update={'pause_seconds':spoken_blocks[0].pause_seconds + moved_pauses}), *spoken_blocks[1:])
    return ShortVideoContent(**base, hook=hook, spoken_blocks=spoken_blocks,
        visual_support=tuple(VisualSupport(**block.model_dump()) for block in visual),
        estimated_duration_seconds=words * 60 / 130 + sum(block.pause_seconds for block in spoken_blocks))
