"""Smoke pago opcional; não participa da suíte offline."""
import os
from uuid import uuid4

import pytest

from suno_asset.domain.models import Evidence, EvidenceRef
from suno_asset.services.groq import GroqExtractor, make_session


@pytest.mark.live
def test_groq_strict_extraction_smoke() -> None:
    if os.getenv('RUN_GROQ_LIVE') != '1':
        pytest.skip('defina RUN_GROQ_LIVE=1 para uma chamada paga ao Groq')
    text = 'O Copom decidiu reduzir a Selic para 13,75% ao ano.'
    ref = EvidenceRef(document_id=uuid4(), chunk_id='live-smoke', parser_version='smoke-v1',
                      page=None, start=0, end=len(text))
    evidence = (Evidence(evidence_id='live-smoke', ref=ref, text=text),)
    session = make_session()
    anchors = GroqExtractor(session).extract(evidence)
    assert anchors and any(anchor.evidence_refs == (ref,) for anchor in anchors)
    assert session.calls == 1
