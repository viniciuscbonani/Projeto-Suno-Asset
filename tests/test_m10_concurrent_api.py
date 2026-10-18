"""Overlapping HTTP workflows must retain their own content, references and events."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

from fastapi.testclient import TestClient

from suno_asset.api.app import create_app
from suno_asset.services.fakes import fake_services
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.parsing import ManualParser


def test_different_http_requests_overlap_without_mixing_workflow_state(tmp_path):
    overlap = Barrier(2)
    class ConcurrentParser:
        def parse(self, raw, document):
            overlap.wait(timeout=10)
            return ManualParser().parse(raw, document)

    store = LocalStorage(tmp_path)
    client = TestClient(create_app(storage=store, services_factory=lambda storage:
        replace(fake_services(storage), parser=ConcurrentParser())))
    texts = ['O Copom decidiu reduzir a Selic para 13,75% ao ano.',
             'O Copom decidiu manter a Selic em 12,50% ao ano.']
    sources = [client.post('/documents', json={'text': text}).json() for text in texts]
    def generate(index):
        return client.post('/generations', json={'source_id': sources[index]['source_id']},
                           headers={'Idempotency-Key': f'concurrent-api-{index}'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(generate, range(2)))
    assert all(response.status_code == 200 for response in responses)
    results = [response.json() for response in responses]
    assert len({r['request_id'] for r in results}) == len({r['document_id'] for r in results}) == 2
    for index, result in enumerate(results):
        assert result['status'] == 'approved'
        assert result['content']['synthesis'] == texts[index]
        assert all(ref['document_id'] == result['document_id'] for ref in result['references'])
        evidence = client.get(f"/generations/{result['request_id']}/evidence/0").json()
        assert evidence['text'] == texts[index]
        row = store.repository.get_run(result['request_id'])
        assert row['source_id'] == sources[index]['source_id'] and row['state'] == 'completed'
        events = store.repository.get_events(result['request_id'])
        assert events and all(event['request_id'] == result['request_id'] for event in events)
