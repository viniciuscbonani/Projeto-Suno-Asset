"""Contrato HTTP local do vertical slice; sem credenciais ou rede."""
from dataclasses import replace
from io import BytesIO
from uuid import UUID

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from suno_asset.api.app import create_app
from suno_asset.services.deterministic import DeterministicGenerator
from suno_asset.services.fakes import FakeEvaluator, fake_services
from suno_asset.services.local_storage import LocalStorage, MAX_PDF_BYTES
from suno_asset.services.parsing import ManualParser
from suno_asset.services.retrieval import DeterministicExtractor, LexicalRetriever


def pdf(text: str) -> bytes:
    output = BytesIO()
    page = canvas.Canvas(output)
    page.drawString(50, 750, text)
    page.save()
    return output.getvalue()


def test_text_upload_generation_result_evidence_and_restart(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    client = TestClient(create_app(storage=storage))
    source = client.post('/documents', json={'text': 'O Copom decidiu reduzir a Selic para 13,75% ao ano. Há riscos de alta e baixa.'})
    assert source.status_code == 201
    created = client.post('/generations', json={'source_id': source.json()['source_id']})
    assert created.status_code == 200
    result = created.json()
    assert result['mode'] == 'simulated' and result['status'] == 'approved'
    assert result['fact_verification']['approved'] is True
    assert result['evaluation']['approved'] is True
    assert result['references'][0]['page'] is None
    request_id = result['request_id']
    assert client.get(f'/generations/{request_id}').json() == result
    evidence = client.get(f'/generations/{request_id}/evidence/0')
    assert evidence.status_code == 200
    assert evidence.json()['ref'] == result['references'][0]
    assert client.get(f'/generations/{request_id}/evidence/999').status_code == 404
    assert TestClient(create_app(storage=storage)).get(f'/generations/{request_id}').json() == result
    assert storage.load_source(UUID(source.json()['source_id'])).origin.value == 'text'


def test_recent_generations_are_listed_from_persistent_repository(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    source = storage.put_text('Ata de teste persistida.')
    row, _ = storage.repository.claim(str(source.source_id), 'f' * 64, 'simulated', configuration={
        'audience':'beginner','output_format':'analytical_text','mode':'simulated'})
    storage.repository.fail_run(row['request_id'], 'generation_unavailable')
    app = create_app(storage=storage)
    route = next(route for route in app.routes if getattr(route, 'path', None) == '/generations'
                 and 'GET' in route.methods)

    response = route.endpoint(50)

    assert response['items'] == [{
        'request_id':row['request_id'],'name':'Texto da ata','date':row['created_at'],
        'status':'failed','state':'errored','audience':'beginner','output_format':'analytical_text',
        'mode':'simulated','source_id':str(source.source_id),
    }]


def test_pdf_upload_and_invalid_inputs(tmp_path) -> None:
    client = TestClient(create_app(storage=LocalStorage(tmp_path)))
    raw = pdf('Copom decidiu reduzir a Selic para 13,75% ao ano.')
    good = client.post('/documents', content=raw, headers={'content-type': 'application/pdf', 'x-filename': '../ata.pdf'})
    assert good.status_code == 201
    assert good.json()['original_filename'] == 'ata.pdf'
    result = client.post('/generations', json={'source_id': good.json()['source_id']})
    assert result.status_code == 200
    assert result.json()['references'][0]['page'] == 1
    assert client.post('/documents', content=raw, headers={'content-type': 'application/pdf'}).status_code == 422
    assert client.post('/documents', content=b'nao e pdf', headers={'content-type': 'application/pdf', 'x-filename': 'a.pdf'}).status_code == 400
    assert client.post('/documents', json={'text': ' '}).status_code == 400
    assert client.post('/documents', json={'text': 'ok', 'file': 'x'}).status_code == 422
    assert client.post('/documents', content=b'x', headers={'content-type': 'text/plain'}).status_code == 415
    assert client.post('/documents', content=b'%PDF-' + b'x' * MAX_PDF_BYTES, headers={'content-type': 'application/pdf', 'x-filename': 'a.pdf'}).status_code == 413
    assert client.post('/generations', json={'source_id': good.json()['source_id'], 'audience': 'advanced'}).status_code == 422
    assert client.post('/generations', json={'source_id': good.json()['source_id'], 'output_format': 'short_video_script'}).status_code == 422
    assert client.post('/generations', json={'source_id': good.json()['source_id'], 'mode': 'real'}).status_code == 422
    assert client.post('/generations', json={'source_id': str(UUID(int=123))}).status_code == 404


def test_needs_review_and_failed_are_exposed_without_false_approval(tmp_path) -> None:
    def inconclusive(storage):
        base = fake_services(storage)
        return replace(base, parser=ManualParser(), retriever=LexicalRetriever(),
                       extractor=DeterministicExtractor(), generator=DeterministicGenerator(),
                       evaluator=FakeEvaluator(('inconclusive',)))
    client = TestClient(create_app(storage=LocalStorage(tmp_path), services_factory=inconclusive))
    source = client.post('/documents', json={'text': 'O Copom decidiu reduzir a Selic para 13,75%.'}).json()
    result = client.post('/generations', json={'source_id': source['source_id']}).json()
    assert result['status'] == 'needs_review'
    assert result['evaluation']['approved'] is False
    other = client.post('/documents', json={'text': 'Somente texto sem fatos financeiros.'}).json()
    failed = client.post('/generations', json={'source_id': other['source_id']}).json()
    assert failed['status'] == 'failed'
    assert failed['content'] is None and failed['references'] == []
    assert client.get(f"/generations/{failed['request_id']}/evidence/0").status_code == 404


def test_same_source_with_different_focus_keeps_immutable_evidence(tmp_path) -> None:
    client = TestClient(create_app(storage=LocalStorage(tmp_path)))
    source = client.post('/documents', json={'text': 'O Copom decidiu reduzir a Selic para 13,75%. Há riscos de alta e de baixa.'}).json()
    first = client.post('/generations', json={'source_id': source['source_id'], 'editorial_focus': 'Selic'}).json()
    second = client.post('/generations', json={'source_id': source['source_id'], 'editorial_focus': 'riscos'}).json()
    assert first['status'] == second['status'] == 'approved'
    assert first['document_id'] == second['document_id']
