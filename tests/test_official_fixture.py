"""Conferência opcional da ata anotada; nunca baixa arquivos durante a suíte."""
from hashlib import sha256
import os
from pathlib import Path
from uuid import UUID

import pytest

from suno_asset.services.ingestion import parse_document, register_document
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.parsing import ManualParser
from suno_asset.services.retrieval import LexicalRetriever, DeterministicExtractor
from suno_asset.services.deterministic import DeterministicEvaluator, DeterministicFactVerifier, DeterministicGenerator
from suno_asset.domain.models import Audience, GenerationRequest, OutputFormat
from suno_asset.workflow.graph import SIMULATED_POLICY

EXPECTED_SHA256 = 'f03423dc25625aa14ae29c10fdacf7b4665b6714ab0667c0dc86a6f77c43665c'
MARKERS = {
    'C01': (3, ('2. Em relação ao cenário doméstico', 'moderação gradual')),
    'C02': (3, ('3. As expectativas de inflação', '4,9%', '4,3%')),
    'C03': (4, ('10. As expectativas de inflação', 'restrição')),
    'C04': (4, ('12. No cenário de referência', '5,2%', '3,9%', '3,2%')),
    'C05': (4, ('13. Os riscos para a inflação', 'alta', 'baixa')),
    'C06': (5, ('15. O Comitê avalia', 'contracionista')),
    'C07': (5, ('16. Choques de oferta', 'segunda ordem')),
    'C08': (5, ('17. No contexto atual', 'calibração')),
    'C09': (5, ('18. O Copom decidiu reduzir', '13,75%')),
    'C10': (5, ('19. O cenário atual', 'incerteza')),
    'C11': (5, ('20. Votaram por essa decisão', 'Gabriel')),
    'C12': (6, ('IPCA 5,2 3,9 3,2', 'IPCA livres 5,2 4,1 3,2')),
}


def test_annotated_critical_fact_retrieval(tmp_path: Path) -> None:
    filename = os.environ.get('SUNO_COPOM281_PDF')
    if not filename:
        pytest.skip('defina SUNO_COPOM281_PDF para a conferência sem rede da ata oficial')
    raw = Path(filename).read_bytes()
    assert sha256(raw).hexdigest() == EXPECTED_SHA256, 'PDF diferente da versão anotada'
    storage = LocalStorage(tmp_path)
    source = storage.put_pdf(raw, filename='Copom281.pdf')
    doc, parsed = parse_document(storage, ManualParser(), register_document(storage, source))
    retriever = LexicalRetriever()
    _, evidence = retriever.retrieve(doc, parsed.units, None)
    retrieved = {page: '\n'.join(item.text for item in evidence if item.ref.page == page)
                 for page in range(1, 7)}
    found = {fact: all(marker in retrieved[page] for marker in markers)
             for fact, (page, markers) in MARKERS.items()}
    assert all(found.values()), f'cobertura de recuperação {sum(found.values())}/12; ausentes: {[key for key, ok in found.items() if not ok]}'
    assert any(item.ref.page == 5 and 'riscos de baixa' in item.text.lower() for item in evidence)
    assert '1 A menos de menção explícita' in retrieved[3]
    assert '2 Valor foi obtido' in retrieved[4]
    assert 'curva de juros. Uma política fiscal' in retrieved[4]
    anchors = DeterministicExtractor().extract(evidence)
    verified = DeterministicFactVerifier().verify(anchors, evidence, SIMULATED_POLICY)
    assert verified.approved
    request = GenerationRequest(request_id=UUID(int=77), source=source,
                                audience=Audience.BEGINNER, output_format=OutputFormat.ANALYTICAL_TEXT)
    content = DeterministicGenerator().generate(request, anchors, evidence)
    assert DeterministicEvaluator().evaluate(content, anchors, evidence, SIMULATED_POLICY).approved
    output = content.development
    assert all(all(marker in output for marker in markers) for _, markers in MARKERS.values())
    assert len(parsed.units) == 6
