"""Verify a local HTTP server, persistent replay, evidence and manual deletion offline."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx


def run():
    with tempfile.TemporaryDirectory(prefix='suno-m10-smoke-') as directory:
        root = Path(directory)
        data = root / 'storage'
        environment = dict(os.environ, SUNO_DATA_ROOT=str(data))
        # The demonstration always uses simulated mode and explicit architecture.
        environment.pop('SUNO_M7_PROMOTION', None)
        environment.pop('GROQ_API_KEY', None)
        with socket.socket() as port_socket:
            port_socket.bind(('127.0.0.1', 0))
            port = port_socket.getsockname()[1]
        address = f'http://127.0.0.1:{port}'
        process = None
        with (root / 'server.log').open('w') as log:
            def start():
                child = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'suno_asset.api.app:app',
                    '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'],
                    env=environment, stdout=log, stderr=log)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError('Local server terminated before readiness')
                    try:
                        if httpx.get(address + '/health', timeout=.5, trust_env=False).status_code == 200:
                            return child
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                child.terminate()
                child.wait(10)
                raise RuntimeError('Local server readiness timeout')

            try:
                process = start()
                with httpx.Client(base_url=address, timeout=30, trust_env=False) as client:
                    source = client.post('/documents', json={
                        'text': 'O Copom decidiu reduzir a Selic para 13,75% ao ano.'})
                    source.raise_for_status()
                    payload = {'source_id': source.json()['source_id'], 'mode': 'simulated',
                               'architecture': 'current', 'model_profile': 'single'}
                    headers = {'Idempotency-Key': 'm10-runtime-smoke'}
                    response = client.post('/generations', json=payload, headers=headers)
                    response.raise_for_status()
                    result = response.json()
                    assert result['status'] == 'approved'
                    request_id = result['request_id']
                    with sqlite3.connect(data / 'runtime.sqlite3') as conn:
                        event_count = conn.execute('SELECT count(*) FROM events').fetchone()[0]
                    process.terminate()
                    process.wait(10)
                    process = start()
                    replay = client.post('/generations', json=payload, headers=headers)
                    assert replay.status_code == 200 and replay.json() == result
                    assert client.get(f'/generations/{request_id}').json() == result
                    assert client.get(f'/generations/{request_id}/evidence/0').status_code == 200
                    assert client.get(f'/generations/{request_id}/status').json()['state'] == 'completed'
                    with sqlite3.connect(data / 'runtime.sqlite3') as conn:
                        assert conn.execute('SELECT count(*) FROM events').fetchone()[0] == event_count
                        assert conn.execute('SELECT count(*) FROM provider_attempts').fetchone()[0] == 0
                    command = [sys.executable, '-m', 'suno_asset.maintenance', '--data-root', str(data),
                               'delete-source', source.json()['source_id']]
                    preview = subprocess.run(command + ['--dry-run'], check=True, capture_output=True,
                                             text=True, env=environment)
                    assert json.loads(preview.stdout)['runs'] == [request_id]
                    deleted = subprocess.run(command + ['--execute'], check=True, capture_output=True,
                                             text=True, env=environment)
                    assert json.loads(deleted.stdout)['state'] == 'deleted'
                    assert client.get(f'/generations/{request_id}').status_code == 410
                    assert client.get(f'/generations/{request_id}/evidence/0').status_code == 410
                    assert client.post('/generations', json=payload, headers=headers).status_code == 410
                    log.flush()
                    lines = (root / 'server.log').read_text().splitlines()
                    events = [json.loads(line) for line in lines if line.startswith('{')]
                    assert any(e.get('node') == 'finalize' and e.get('status') == 'completed' for e in events)
                    assert all('timestamp' in e and e.get('request_id') == request_id for e in events)
                    assert 'O Copom decidiu reduzir' not in (root / 'server.log').read_text()
                    return {'version': 'm10-http-smoke-v1', 'passed': True,
                            'mode': 'simulated', 'loopback_only': True, 'server_restarted': True,
                            'persistent_replay': True, 'events_before_replay': event_count,
                            'events_after_replay': event_count, 'provider_attempts': 0,
                            'evidence_available_before_deletion': True,
                            'structured_logs_emitted': True, 'source_content_in_logs': False,
                            'deleted_resources_http': 410, 'authorized_billing_usd': 0}
            finally:
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run()
    output = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    print(output, end='')


if __name__ == '__main__':
    main()
