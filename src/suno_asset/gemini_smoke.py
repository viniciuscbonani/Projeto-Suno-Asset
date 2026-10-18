"""One explicit structured connectivity probe, separate from pipeline calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from suno_asset.m7_literal_eval import audited_transport
from suno_asset.services.gemini import configuration, make_session
from suno_asset.services.protocols import ExpectedServiceError

SCHEMA = {'type': 'object', 'properties': {'status': {'type': 'string', 'enum': ['ok']}},
    'required': ['status'], 'additionalProperties': False}


def probe(out: Path, *, execute: bool = False) -> dict:
    if not execute:
        return {'status': 'planned', 'calls': 0, 'configuration': configuration()}
    session = make_session()
    session.configure('literal-v1', 'economy')
    session.require_free_quota_evidence()
    session.max_calls = 1
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    session.transport = audited_transport(session, out)
    identity = configuration()
    job_id = f'gemini-connectivity-{uuid4().hex}'
    session.quota_job_id = job_id
    session.quota_ledger.register_job(job_id, sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        'connectivity-json', json.dumps(identity, sort_keys=True))
    session.quota_ledger.start_job(job_id)
    result = {'version': 'gemini-connectivity-v1', 'at': datetime.now(timezone.utc).isoformat(),
        'purpose': 'structured_connectivity_only', 'separate_from_pipeline_budget': True,
        'authorized_billing_usd': 0, 'status': 'failed'}
    try:
        value = session.call('extract', 'Responda somente com o objeto JSON solicitado. Retorne status ok.',
            {'task': 'connectivity'}, SCHEMA, 256)
        result.update(status='completed', response=value)
    except ExpectedServiceError as exc:
        result['error_code'] = exc.code
    except Exception as exc:
        # Do not serialize exception messages that may include credential URLs.
        result.update(error_code='unexpected_error', error_type=type(exc).__name__)
    result.update(calls=session.calls, manifest=session.manifest(), attempts=session.attempt_records,
        reported_tokens=session.prompt_tokens + session.completion_tokens,
        usage_complete=bool(session.calls) and all(a.get('usage_known') is True for a in session.attempt_records),
        unknown_reserved_tokens=sum(a['total_reserved'] for a in session.attempt_records if not a.get('usage_known')))
    path = out / 'result.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    digest = sha256(path.read_bytes()).hexdigest()
    finish = session.quota_ledger.complete_job if result['status'] == 'completed' else session.quota_ledger.incomplete_job
    finish(job_id, path, digest)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='autoriza somente uma tentativa JSON no Gemini gratuito')
    parser.add_argument('--out', type=Path, default=Path('data/gemini-connectivity') /
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    args = parser.parse_args()
    try:
        result = probe(args.out, execute=args.execute)
        print(json.dumps({key: result[key] for key in ('status', 'calls', 'reported_tokens',
            'unknown_reserved_tokens', 'error_code') if key in result}))
        return 0 if result['status'] in {'planned', 'completed'} else 1
    except ExpectedServiceError as exc:
        print(json.dumps({'status': 'blocked', 'error_code': exc.code, 'calls': 0}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
