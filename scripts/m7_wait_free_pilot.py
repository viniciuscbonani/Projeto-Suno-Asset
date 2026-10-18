"""Wait between executions, then run one authorized development pilot once."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import httpx

from suno_asset.m7_acceptance import fingerprint
from suno_asset.m7_literal_eval import _write_summary
from suno_asset.services.groq import make_session
from suno_asset.services.quota_ledger import FreeQuotaLedger, QuotaWindow

ROOT = Path(__file__).resolve().parents[1]
stopping = False


def stop(*_):
    global stopping
    stopping = True


def fingerprint_hash():
    return sha256(json.dumps(fingerprint(ROOT / 'tests/fixtures/evaluation/corpus.json'),
                            ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def refresh_evidence(original, out):
    """Refresh authenticated availability; keep operator attestation explicit.

    The models endpoint does not independently verify the billing plan or TPD.
    Neither limits nor unknown consumption can be raised/cleared here.
    """
    session = make_session(free_only=True)
    if sha256(session.api_key.encode()).hexdigest() != original['credential_sha256']:
        raise ValueError('Credential changed; operator evidence cannot be reused')
    response = httpx.get('https://api.groq.com/openai/v1/models',
                        headers={'Authorization': 'Bearer ' + session.api_key}, timeout=20)
    if response.status_code != 200:
        raise ValueError('Authenticated model availability check failed')
    catalog = response.json()
    available = {row['id'] for row in catalog['data'] if row.get('active', True)}
    if not set(original['models_available']) <= available:
        raise ValueError('Required models unavailable')
    receipt = {'checked_at': datetime.now(timezone.utc).isoformat(), 'status': 200,
               'endpoint': 'https://api.groq.com/openai/v1/models',
               'required_models_available': original['models_available'],
               'provider_response_sha256': sha256(response.content).hexdigest(),
               'billing_plan_independently_verified': False,
               'limits_basis': 'Unchanged operator attestation and conservative documented defaults'}
    receipt_path = out / 'authenticated-availability.json'
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    attachment = Path(original['evidence_file'])
    if not attachment.is_absolute():
        attachment = Path(os.environ['GROQ_FREE_QUOTA_EVIDENCE']).parent / attachment
    if sha256(attachment.read_bytes()).hexdigest() != original['evidence_sha256']:
        raise ValueError('Original operator attestation changed')
    renewed = {**original, 'checked_at': receipt['checked_at'],
               'evidence_file': str(attachment.resolve()),
               'availability_receipt': {'file': str(receipt_path.resolve()),
                                        'sha256': sha256(receipt_path.read_bytes()).hexdigest()},
               'refresh_scope': 'Authenticated availability only; free plan and limits remain user-attested, not a new billing-dashboard observation'}
    renewed['verification_levels'] = {**renewed.get('verification_levels', {}),
                                     'models_available': 'fresh authenticated models catalogue'}
    path = out / 'groq-free-quota-evidence.json'
    path.write_text(json.dumps(renewed, ensure_ascii=False, indent=2) + '\n')
    return path


def _persist_queue(path, state):
    """Persist the no-replay marker durably before creating a provider child."""
    _write_summary(path, state)
    with Path(path).open('r') as handle:
        os.fsync(handle.fileno())
    directory_fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def worker_script_hash():
    return sha256(Path(__file__).read_bytes()).hexdigest()


def process_identity(pid):
    """Linux birth identity prevents PID reuse from authorizing a duplicate."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except FileNotFoundError:
        return None
    fields = stat.rsplit(')', 1)[1].split()
    return {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'start_ticks': fields[19], 'state': fields[0]}


def prior_worker_dead(state):
    current = process_identity(state['pid'])
    if current is None or current['state'] == 'Z':
        return True
    prior = state.get('worker_identity')
    return bool(prior and (prior['boot_id'], prior['start_ticks']) !=
                (current['boot_id'], current['start_ticks']))


def claim_queue(queue_dir, expected_fingerprint, evidence_path, ledger_path):
    """Hold the directory lock until exit; atomically claim one immutable job."""
    queue_dir.mkdir(parents=True, exist_ok=True)
    lock = (queue_dir / 'worker.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = queue_dir / 'queue.json'
        binding = {'fingerprint_sha256': expected_fingerprint,
                   'worker_script_sha256': worker_script_hash(),
                   'evidence_path': str(evidence_path.resolve()),
                   'evidence_sha256': sha256(evidence_path.read_bytes()).hexdigest(),
                   'ledger_path': str(ledger_path.resolve()),
                   'configuration_id': 'literal-quality',
                   'source_id': 'copom281-20260916', 'authorized_billing_usd': 0,
                   'result_dir': str((queue_dir / 'pilot').resolve())}
        state = {'version': 'm7-free-pilot-queue-v3', **binding,
                 'pid': os.getpid(), 'worker_identity': process_identity(os.getpid()),
                 'status': 'waiting_quota', 'configuration_id': 'literal-quality',
                 'source_id': 'copom281-20260916', 'provider_execution_started': False,
                 'authorized_billing_usd': 0,
                 'admission_policy': 'available_daily_balance_per_request',
                 'result_dir': str((queue_dir / 'pilot').resolve())}
        try:
            with state_path.open('x') as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            directory_fd = os.open(queue_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except FileExistsError:
            prior = json.loads(state_path.read_text())
            if any(prior.get(key) != value for key, value in binding.items()):
                raise ValueError('Queue binding changed; manual review required')
            if prior.get('provider_execution_started') is not False or prior.get('status') not in (
                    'waiting_quota', 'stopped_before_execution'):
                raise ValueError('Execution may have started; manual review required, never replay')
            if not prior_worker_dead(prior):
                raise ValueError('Prior worker is alive or its death is unproven')
            state = {**prior, 'pid': os.getpid(), 'worker_identity': process_identity(os.getpid()),
                     'status': 'waiting_quota',
                     'resumes': [*prior.get('resumes', []), {'prior_pid': prior['pid'],
                         'at': datetime.now(timezone.utc).isoformat()}]}
            _persist_queue(state_path, state)
        return lock, state_path, state
    except BaseException:
        lock.close()
        raise


def quota_state(ledger, now):
    """Wait only for exhausted daily quota; each actual request reserves its usage.

    Existing reported or unknown usage reduces the available balance. It does
    not require an otherwise usable daily window to become completely empty.
    """
    snapshot = ledger.snapshot(now=now)
    used = snapshot['reported_tokens'] + snapshot['unknown_reserved_tokens']
    blocked = []
    with ledger._connect() as db:
        rows = db.execute('''SELECT created_at,reserved_tokens,actual_tokens,status
            FROM attempts WHERE created_at>=? ORDER BY created_at''',
            ((now - timedelta(days=1)).isoformat(),)).fetchall()
    if used >= ledger.window.tokens_per_day:
        blocked.append(ledger._token_release(rows, 1, ledger.window.tokens_per_day, 86400))
    if snapshot['attempts_24h'] >= ledger.window.requests_per_day:
        index = len(rows) - ledger.window.requests_per_day
        blocked.append(datetime.fromisoformat(rows[index]['created_at']) + timedelta(days=1))
    window = max(blocked).isoformat() if blocked else now.isoformat()
    return snapshot, used, window


def unstarted_deferral(ledger, args, report, summary, exit_code):
    """Prove a closed child only deferred quota and never attempted transport."""
    from suno_asset.m7_literal_eval import configuration, make_execution_protocol
    logical = {'version': 'm7-full-ata-job-v2', 'purpose': 'main',
        'fingerprint': args.fingerprint, 'source_id': 'copom281-20260916',
        'configuration': 'literal-quality', 'audience': 'beginner',
        'output_format': 'analytical_text', 'repetition': 1}
    job_id = sha256(json.dumps(logical, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    protocol = make_execution_protocol('main', 2,
        {'extract': .06, 'verify': .05, 'generate': .03, 'evaluate': .06, 'refine': .06})
    report_fingerprint = sha256(json.dumps(report.get('fingerprint'),
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if (exit_code != 0 or report.get('status') != 'quota_deferred' or report.get('rows') != []
        or report_fingerprint != args.fingerprint
        or report.get('execution_protocol') != protocol
        or report.get('configuration_specs') != [configuration('literal-quality')]
        or report.get('max_refinements') != 2 or report.get('max_cost_usd') != .15
        or report.get('stage_cost_caps_usd') != protocol['stage_cost_limits']
        or report.get('paid') is not False or report.get('matrix') is not False
        or report.get('provider_calls_real') is not True or report.get('quota_mode') != 'free_only'
        or report.get('authorized_billing_usd') != 0 or report.get('heldout_included') is not False):
        raise ValueError('Unstarted quota deferral lacks matching closed execution evidence')
    with ledger._connect() as db:
        job = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        attempts = db.execute('SELECT COUNT(*) FROM attempts WHERE job_id=?', (job_id,)).fetchone()[0]
    window = ledger.next_available_window(job_id)
    if not job or job['status'] != 'queued' or attempts or not window:
        raise ValueError('Quota deferral cannot prove zero provider attempts')
    identity = json.loads(job['configuration_id'])
    if (any(identity.get(key) != value for key, value in logical.items())
        or identity.get('execution_protocol') != protocol or job['fingerprint'] != args.fingerprint
        or job['source_id'] != logical['source_id']):
        raise ValueError('Deferred job identity changed')
    return {'job_id': job_id, 'attempts': 0, 'child_exit_code': exit_code,
            'summary_file': str(summary.resolve()), 'summary_sha256': sha256(summary.read_bytes()).hexdigest(),
            'next_window': window, 'closed_at': datetime.now(timezone.utc).isoformat()}


def main(argv=None):
    global stopping
    stopping = False
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue-dir', type=Path, required=True)
    parser.add_argument('--fingerprint', required=True)
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    os.environ['GROQ_FREE_ONLY'] = '1'
    evidence_path = Path(os.environ['GROQ_FREE_QUOTA_EVIDENCE'])
    evidence = json.loads(evidence_path.read_text())
    if evidence['account_plan'] != 'free' or evidence['zero_billing_confirmed'] is not True:
        raise ValueError('Free-only attestation absent')
    limits = QuotaWindow(**evidence['limits'])
    if limits != QuotaWindow():
        raise ValueError('Replan explicitly if the configured conservative limits change')
    ledger_path = Path(os.environ['GROQ_FREE_QUOTA_DB'])
    ledger = FreeQuotaLedger(ledger_path, limits)
    lock, state_path, state = claim_queue(args.queue_dir, args.fingerprint, evidence_path, ledger_path)
    try:
        while True:
            run_worker(args, evidence, ledger, state_path, state)
            if state['status'] != 'waiting_quota' or state['provider_execution_started'] or stopping:
                break
            # Another execution may exhaust quota during availability refresh.
            time.sleep(30)
    finally:
        lock.close()


def run_worker(args, evidence, ledger, state_path, state):
    limits = ledger.window
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    previous_window = None
    while not stopping:
        if fingerprint_hash() != args.fingerprint or worker_script_hash() != state['worker_script_sha256']:
            state.update(status='code_changed', reason='Candidate fingerprint changed before execution')
            _persist_queue(state_path, state)
            return
        now = datetime.now(timezone.utc)
        snapshot, used, window = quota_state(ledger, now)
        deferred_until = state.get('deferred_until')
        if deferred_until and datetime.fromisoformat(deferred_until) > now:
            window = max(datetime.fromisoformat(window), datetime.fromisoformat(deferred_until)).isoformat()
        state.update(quota=snapshot, next_window_available_at=window,
                     checked_at=now.isoformat(), remaining_tokens=limits.tokens_per_day - used)
        _persist_queue(state_path, state)
        available = (used < limits.tokens_per_day and snapshot['attempts_24h'] < limits.requests_per_day
                     and (not deferred_until or datetime.fromisoformat(deferred_until) <= now))
        if available or window != previous_window:
            print(json.dumps({'status': 'quota_available' if available else 'waiting_quota', 'available_at': window,
                              'remaining_tokens': state['remaining_tokens']}), flush=True)
            previous_window = window
        if available:
            break
        time.sleep(30)  # Between executions; the pipeline's clock has not begun.
    if stopping:
        state.update(status='stopped_before_execution')
        _persist_queue(state_path, state)
        return
    try:
        fresh = refresh_evidence(evidence, args.queue_dir)
        if fingerprint_hash() != args.fingerprint:
            raise ValueError('Fingerprint changed during availability verification')
        # Availability refresh is not an account quota reservation. Recheck the
        # shared ledger; the child still reserves every attempt transactionally.
        now = datetime.now(timezone.utc)
        snapshot, used, window = quota_state(ledger, now)
        state.update(quota=snapshot, next_window_available_at=window,
                     checked_at=now.isoformat(), remaining_tokens=limits.tokens_per_day - used)
        if used >= limits.tokens_per_day or snapshot['attempts_24h'] >= limits.requests_per_day:
            state.update(status='waiting_quota', reason='Daily quota exhausted during availability check')
            _persist_queue(state_path, state)
            return
        state['refreshed_evidence_sha256'] = sha256(fresh.read_bytes()).hexdigest()
        env = {**os.environ, 'GROQ_FREE_QUOTA_EVIDENCE': str(fresh.resolve())}
        deferrals = state.get('unstarted_deferrals', [])
        result_dir = Path(state['result_dir'])
        log_file = args.queue_dir / 'pilot.log'
        if deferrals:
            result_dir = result_dir.with_name(f'{result_dir.name}-after-deferral-{len(deferrals)}')
            log_file = args.queue_dir / f'pilot-after-deferral-{len(deferrals)}.log'
        command = [str(ROOT / '.venv/bin/python'), '-m', 'suno_asset.m7_literal_eval',
                   '--free-only', '--configurations', 'literal-quality', '--source-id', 'copom281-20260916',
                   '--repeats', '1', '--max-refinements', '2', '--max-runs', '1', '--max-cost-usd', '0.15',
                   '--stage-cost-cap', 'extract=0.06', '--stage-cost-cap', 'verify=0.05',
                   '--stage-cost-cap', 'generate=0.03', '--stage-cost-cap', 'evaluate=0.06',
                   '--stage-cost-cap', 'refine=0.06', '--out', str(result_dir)]
        state.update(status='running', provider_execution_started=True,
                     execution_started_at=datetime.now(timezone.utc).isoformat(), command=command,
                     active_result_dir=str(result_dir), active_log_file=str(log_file))
        _persist_queue(state_path, state)
        with log_file.open('x') as log:
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            state.update(child_pid=child.pid, child_identity=process_identity(child.pid))
            _persist_queue(state_path, state)
            while child.poll() is None:
                if stopping:
                    child.terminate()
                    try:
                        child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=30)
                    break
                time.sleep(5)
        state.update(status='stopped' if stopping else 'finished_pending_review', exit_code=child.returncode,
                     finished_at=datetime.now(timezone.utc).isoformat())
        summary = result_dir / 'summary.json'
        if summary.is_file():
            report = json.loads(summary.read_text())
            state['outcomes'] = [{key: row.get(key) for key in
                                 ('status','errors','technical_complete','processing_complete','attempts')}
                                for row in report.get('rows', [])]
            state['summary_sha256'] = sha256(summary.read_bytes()).hexdigest()
            if not stopping and report.get('status') == 'quota_deferred':
                proof = unstarted_deferral(ledger, args, report, summary, child.returncode)
                state.update(status='waiting_quota', provider_execution_started=False,
                    unstarted_deferrals=[*deferrals, proof],
                    deferred_until=proof['next_window']['available_at'],
                    next_window_available_at=proof['next_window']['available_at'],
                    reason='Quota deferred before any provider attempt; closed invocation preserved')
    except Exception as exc:
        # No retry of an uncertain/incomplete execution and no secret-bearing
        # exception text in the worker state. Keep all checkpoints unchanged.
        state.update(status='halted', error_type=type(exc).__name__)
    _persist_queue(state_path, state)
    print(json.dumps({'status': state['status'], 'outcomes': state.get('outcomes')}), flush=True)


if __name__ == '__main__':
    main()
