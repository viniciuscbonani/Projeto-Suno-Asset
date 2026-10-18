"""Explicit, audited successors for terminated M7 tasks; never replay uncertainty."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path

from suno_asset.services.quota_ledger import FreeQuotaLedger


def validate_recovery_lineage(row):
    """Validate portable predecessor evidence without requiring the live DB."""
    lineage = row.get('quota_recovery_lineage')
    if lineage is None:
        if row.get('quota_root_job_id') is not None or str(row.get('quota_job_id', '')).startswith('recovery-'):
            raise ValueError('Execução sucessora sem linhagem auditada')
        return
    if not isinstance(lineage, list) or not lineage or not row.get('quota_root_job_id'):
        raise ValueError('Linhagem de recuperação ausente/inválida')
    predecessor = row['quota_root_job_id']
    for link in lineage:
        evidence = json.loads(link['evidence_json'])
        job = evidence['job']
        expected_id = 'recovery-' + sha256(('m7-recovery-v1:' + predecessor + ':' + job['result_sha256']).encode()).hexdigest()
        if (link['predecessor_id'] != predecessor or job['job_id'] != predecessor
            or link['successor_id'] != expected_id or not isinstance(link.get('reason'), str)
            or not link['reason'].strip() or job['source_id'] != row.get('source_id')
            or job['fingerprint'] != row.get('quota_fingerprint_sha256')):
            raise ValueError('Identidade/justificativa da linhagem divergente')
        identity = json.loads(job['configuration_id'])
        if row.get('quota_identity') is not None:
            if row['quota_identity'] != identity:
                raise ValueError('Identidade da sucessora difere da predecessora')
        elif identity.get('version') == 'm7-full-ata-job-v2':
            for identity_key, result_key in (('configuration', 'configuration_id'),
                ('audience', 'audience'), ('output_format', 'output_format'),
                ('repetition', 'repetition'), ('execution_protocol', 'execution_protocol')):
                if identity.get(identity_key) != row.get(result_key):
                    raise ValueError('Configuração da sucessora difere da predecessora')
        actual = FreeQuotaLedger._closed_job_evidence(None, job, recorded_attempts=evidence['attempts'])
        if actual != evidence:
            raise ValueError('Evidência da linhagem alterada/incompleta')
        predecessor = link['successor_id']
    if predecessor != row.get('quota_job_id'):
        raise ValueError('Linhagem não termina na execução declarada')
    if 'quota_recovery_nominal_usd' in row:
        expected_cost = sum(json.loads(link['evidence_json'])['nominal_usd'] for link in lineage)
        if (type(row['quota_recovery_nominal_usd']) not in (float, int)
            or row['quota_recovery_nominal_usd'] != expected_cost):
            raise ValueError('Custo declarado da linhagem divergente')


def recovery_cost(row):
    validate_recovery_lineage(row)
    return sum(json.loads(link['evidence_json'])['nominal_usd']
        for link in row.get('quota_recovery_lineage', []))


def campaign_admission_cost(ledger, job_ids, per_job_ceiling):
    """Conservative pending budget plus settled history, once per execution."""
    from suno_asset.m7_eval import PRICES
    from suno_asset.services.groq import _reported_usage, _attempt_token_charge
    total = 0.0
    counted = set()
    for root_id in dict.fromkeys(job_ids):
        job = ledger.resolve_job(root_id)
        if job is None:
            total += per_job_ceiling
            continue
        for link in ledger.recovery_lineage(job['job_id']):
            if link['predecessor_id'] not in counted:
                total += json.loads(link['evidence_json'])['nominal_usd']
                counted.add(link['predecessor_id'])
        if job['job_id'] in counted:
            continue
        counted.add(job['job_id'])
        if job['status'] in {'running', 'incomplete'}:
            raise ValueError('Tarefa iniciada sem sucessora auditada; não repetir chamadas incertas')
        if job['status'] != 'complete':
            total += per_job_ceiling
            continue
        row = ledger._read_hashed(job['result_path'], job['result_sha256'])
        validate_recovery_lineage(row)
        attempts = row.get('attempts')
        if type(attempts) is int and row.get('budget_file'):
            attempts = ledger._read_hashed(row['budget_file'], row.get('budget_sha256')).get('attempts')
        if not isinstance(attempts, list):
            # Older/partial metadata cannot prove a lower nominal cost.
            total += per_job_ceiling
            continue
        for attempt in attempts:
            if attempt.get('status') == 'deferred':
                continue
            usage = _reported_usage(attempt) if attempt.get('usage_known') is True else None
            ip, op = PRICES[attempt['model']]
            if usage is None:
                total += _attempt_token_charge(attempt) * max(ip, op) / 1e6
            else:
                total += (usage[0] * ip + usage[1] * op) / 1e6
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path,
        default=Path(os.getenv('GROQ_FREE_QUOTA_DB', 'data/m7-quota/free-quota.sqlite3')))
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    try:
        successor = FreeQuotaLedger(args.ledger).create_successor(args.job_id, reason=args.reason)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(successor, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
