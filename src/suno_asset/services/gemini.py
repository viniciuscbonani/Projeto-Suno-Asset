"""Gemini adapter for the existing synchronous, structured session contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import math
from pathlib import Path
from threading import Lock
import time
from urllib.error import HTTPError, URLError

from suno_asset.services.groq import (GroqSession, STAGE_OUTPUT, REASONING_EFFORT,
    chat_completion_transport, _reported_usage, _attempt_token_charge)
from suno_asset.services.gemini_quota import GeminiQuotaLedger, GeminiQuotaWindow
from suno_asset.services.llm_budget import RateGate, RequestBudget, TokenCounter, seconds
from suno_asset.services.pricing import PRICES, GEMINI_PRICE_SOURCE, GEMINI_PRICE_VALID_UNTIL
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.services.provider_config import environment_value
from suno_asset.services.structured_json import parse_json, validate_json

ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
MODEL = 'gemini-3.8-flash'
SUPPORTED_MODELS = frozenset({MODEL, 'gemini-3.6-flash', 'gemini-3.5-flash-lite'})
_GATES: dict[str, RateGate] = {}
_GATES_LOCK = Lock()


@dataclass(frozen=True)
class GeminiRequestBudget(RequestBudget):
    provider_tokens_per_minute: int | None = None

    def as_dict(self):
        result=super().as_dict()
        result['planning_token_ceiling']=self.token_limit
        result['tpm_limit']=self.provider_tokens_per_minute
        return result


def _default_transport(payload, api_key, timeout):
    return chat_completion_transport(ENDPOINT, payload, api_key, timeout, provider='Gemini')


def configured_limits() -> GeminiQuotaWindow:
    try:
        return GeminiQuotaWindow(**{field: int(raw) if raw else None for field, raw in (
            ('requests_per_minute', environment_value('GEMINI_RPM_LIMIT')),
            ('requests_per_day', environment_value('GEMINI_RPD_LIMIT')),
            ('tokens_per_minute', environment_value('GEMINI_TPM_LIMIT')),
            ('tokens_per_day', environment_value('GEMINI_TPD_LIMIT')))})
    except ValueError as exc:
        raise ExpectedServiceError('gemini_config_invalid', 'cotas Gemini exigem inteiros positivos') from exc


def configuration() -> dict:
    model = environment_value('GEMINI_MODEL', MODEL).strip()
    if model not in SUPPORTED_MODELS:
        code = 'llm_price_unknown' if model not in PRICES else 'gemini_model_unsupported'
        raise ExpectedServiceError(code, 'modelo Gemini sem contrato/preço validado; não haverá substituição automática')
    limits = configured_limits()
    return {'provider': 'gemini', 'models': {stage: model for stage in STAGE_OUTPUT},
        'reasoning_effort': 'minimal' if model == 'gemini-3.5-flash-lite' else REASONING_EFFORT,
        'provider_adapter_version': 'gemini-openai-v1',
        'free_only': environment_value('GEMINI_FREE_ONLY') == '1',
        'account_plan': 'free_declared_by_operator', 'account_plan_verified': False,
        'authorized_billing_usd': 0,
        'provider_limits': {'version': 'gemini-limits-v1', 'configured': limits.__dict__,
            'observed': None, 'account_scope': environment_value('GEMINI_QUOTA_SCOPE', 'local-project'),
            'provider_day_window': 'unknown', 'local_day_window_seconds': 86400},
        'nominal_prices': {'input_per_million_usd': PRICES[model][0],
            'output_including_reasoning_per_million_usd': PRICES[model][1],
            'source': GEMINI_PRICE_SOURCE, 'checked_at': '2026-10-17',
            'valid_until': GEMINI_PRICE_VALID_UNTIL}}


def normalized_usage(raw) -> dict:
    usage = raw.get('usage') if isinstance(raw, dict) else None
    reported = _reported_usage(usage)
    def detail(container, name):
        details = usage.get(container) if isinstance(usage, dict) else None
        value = details.get(name) if isinstance(details, dict) else None
        return value if type(value) is int and value >= 0 else None
    reasoning = detail('completion_tokens_details', 'reasoning_tokens')
    cached = detail('prompt_tokens_details', 'cached_tokens')
    # OpenAI-compatible completion_tokens INCLUDES reasoning. Never add it
    # again. Inconsistent details cannot release an unknown reservation.
    if reported and (reasoning is not None and reasoning > reported[1]
                     or cached is not None and cached > reported[0]):
        reported = None
    return {'usage_known': reported is not None,
        'prompt_tokens': reported[0] if reported else None,
        'completion_tokens': reported[1] if reported else None,
        'total_tokens': sum(reported) if reported else None,
        'reasoning_tokens': reasoning, 'cached_tokens': cached,
        'visible_output_tokens': reported[1] - reasoning if reported and reasoning is not None else None,
        'raw_usage': usage}


def _retry_seconds(headers, error) -> float | None:
    values = []
    value = headers.get('Retry-After')
    if value:
        try:
            values.append(float(value))
        except ValueError:
            try:
                values.append((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    details = error.get('details', []) if isinstance(error, dict) else []
    for detail in details if isinstance(details, list) else []:
        if isinstance(detail, dict) and str(detail.get('@type', '')).endswith('google.rpc.RetryInfo'):
            delay = seconds(detail.get('retryDelay'))
            if delay is not None:
                values.append(delay)
    valid = [max(0, value) for value in values if math.isfinite(value)]
    return max(valid) if valid else None


@dataclass
class GeminiSession(GroqSession):
    api_key: str = field(repr=False)
    model: str = MODEL
    transport: object = _default_transport
    profile: str = 'economy'
    architecture: str = 'literal-v1'
    free_only: bool = False
    # The Groq tokenizer is not a Gemini tokenizer. Use the existing byte
    # upper bound for planning, report only provider counts as actual usage.
    counter: TokenCounter = field(default_factory=lambda: TokenCounter(None))
    limits: GeminiQuotaWindow = field(default_factory=GeminiQuotaWindow)
    account_scope: str = 'local-project'
    nominal_execution_cap_usd: float = .15

    @property
    def provider(self):
        return 'gemini'

    def __post_init__(self):
        if self.model not in SUPPORTED_MODELS:
            raise ExpectedServiceError('gemini_model_unsupported', 'modelo Gemini sem contrato validado')
        self.tpm_limit = self.limits.tokens_per_minute
        self.rpm_limit = self.limits.requests_per_minute
        self.rpd_limit = self.limits.requests_per_day
        self.tpd_limit = self.limits.tokens_per_day
        self.scope_id = sha256(self.account_scope.encode()).hexdigest()
        self.resolved_identity = configuration()
        if self.quota_ledger is None:
            root = Path(environment_value('GEMINI_FREE_QUOTA_DIR', 'data/m7-quota/gemini'))
            self.quota_ledger = GeminiQuotaLedger(root / f'{self.scope_id}.sqlite3', self.limits, scope=self.scope_id)
        if (not isinstance(self.quota_ledger, GeminiQuotaLedger) or self.quota_ledger.scope != self.scope_id
            or self.quota_ledger.window != self.limits):
            raise ExpectedServiceError('gemini_config_invalid', 'Gemini exige ledger próprio do escopo configurado')
        if self.rate_gate is None:
            with _GATES_LOCK:
                self.rate_gate = _GATES.setdefault(self.scope_id, RateGate()) if self.transport is _default_transport else RateGate(enforce_window=False)

    def model_for(self, stage):
        if stage not in STAGE_OUTPUT:
            raise ValueError('etapa de LLM inválida')
        if self.model_overrides.get(stage, self.model) != self.model:
            raise ExpectedServiceError('gemini_model_unsupported', 'troca automática de modelo Gemini proibida')
        return self.model

    def repair_model_for(self, stage):
        return self.model_for(stage)

    def configure(self, architecture, profile):
        if architecture != 'literal-v1' or profile != 'economy':
            raise ExpectedServiceError('gemini_profile_invalid', 'Gemini disponível somente em literal-v1 + economy')
        super().configure(architecture, profile)

    @property
    def effective_limit(self):
        # Inherited preflight arithmetic needs an operational planning ceiling.
        # This fallback is the existing context ceiling, not an inferred TPM.
        return min(self.tpm_limit, self.context_limit) if self.tpm_limit else self.context_limit

    def payload(self, stage, system, data, schema, output_cap=None):
        payload = super().payload(stage, system, data, schema, output_cap)
        if self.model in {'gemini-3.6-flash', 'gemini-3.5-flash-lite'}:
            payload.pop('temperature', None)
        if self.model == 'gemini-3.5-flash-lite':
            # The model defaults to minimal thinking, which is recommended for
            # high-volume extraction; omit the override to preserve that default.
            payload.pop('reasoning_effort', None)
        payload['max_tokens'] = payload.pop('max_completion_tokens')
        return payload

    def budget(self, stage, system, data, schema, output_cap=None):
        payload = self.payload(stage, system, data, schema, output_cap)
        return GeminiRequestBudget(self.counter.estimate(payload), payload['max_tokens'],
            self.effective_limit, self.context_limit,provider_tokens_per_minute=self.tpm_limit)

    def manifest(self):
        identity = {**self.resolved_identity}
        identity.update(models={stage: self.model_for(stage) for stage in STAGE_OUTPUT}, free_only=self.free_only)
        identity['provider_limits'] = {**identity['provider_limits'], 'configured': self.limits.__dict__,
            'account_scope': self.account_scope, 'observed': self.quota_ledger.snapshot()['observations']}
        return {**identity, 'version': 'execution-manifest-v2', 'architecture': self.architecture,
            'profile': self.profile, 'prompt_version': self.prompt_version,
            'models_used': {stage: sorted({a['model'] for a in self.attempt_records if a['stage'] == stage}) for stage in STAGE_OUTPUT},
            'max_calls': self.max_calls, 'max_tokens': self.max_tokens, 'deadline_seconds': self.deadline_seconds,
            'context_limit': self.context_limit, 'counter': self.counter.mode,
            'tpm_limit': self.tpm_limit, 'planning_token_ceiling': self.effective_limit,
            'stage_cost_limits_usd': self.stage_cost_limits,
            'nominal_execution_cap_usd': self.nominal_execution_cap_usd,
            'token_accounting': 'reported_usage_plus_unknown_reservations',
            'quota_mode': 'free_only' if self.free_only else 'blocked', 'quota_job_id': self.quota_job_id,
            'persistent_quota_ledger': str(self.quota_ledger.path.resolve()),
            'usage': [a.get('usage') for a in self.attempt_records]}

    def require_free_quota_evidence(self):
        if not self.free_only:
            raise ExpectedServiceError('free_quota_required', 'configure GEMINI_FREE_ONLY=1; cobrança autorizada é zero')
        # This is an operator assertion, explicitly not verification from a key.
        self.quota_evidence = {'provider': 'gemini', 'account_plan': 'free_declared_by_operator',
            'account_plan_verified': False, 'authorized_billing_usd': 0}
        return self.quota_evidence

    def reported_usage(self, raw):
        usage = normalized_usage(raw)
        return (usage['prompt_tokens'], usage['completion_tokens']) if usage['usage_known'] else None

    def nominal_cost(self, *, input_tokens, output_tokens, model=None):
        rates = PRICES.get(model or self.model)
        if rates is None:
            raise ExpectedServiceError('llm_price_unknown', 'preço nominal desconhecido')
        return (input_tokens * rates[0] + output_tokens * rates[1]) / 1e6

    def committed_cost(self, stage=None):
        return sum(self.nominal_cost(input_tokens=a.get('prompt_tokens') if a.get('usage_known') else a['input_estimate'],
            output_tokens=a.get('completion_tokens') if a.get('usage_known') else a['output_reserved'], model=a['model'])
            for a in self.attempt_records if stage is None or a['stage'] == stage)

    def preflight(self, stage, requests, *, future_calls=0):
        budgets = [self.budget(stage, *request) for request in requests]
        if any(not b.fits for b in budgets):
            raise ExpectedServiceError('llm_request_budget', 'pedido indivisível não cabe; fragmentação automática proibida')
        total = sum(b.total for b in budgets)
        cost = sum(self.nominal_cost(input_tokens=b.input_tokens, output_tokens=b.output_tokens) for b in budgets)
        if self.committed_cost() + cost > self.nominal_execution_cap_usd:
            raise ExpectedServiceError('llm_execution_nominal_cap', 'teto nominal agregado de USD 0,15 atingido')
        if stage in self.stage_cost_limits and self.committed_cost(stage) + cost > self.stage_cost_limits[stage]:
            raise ExpectedServiceError('llm_stage_cost_budget', 'teto nominal da etapa atingido')
        if self.calls + len(budgets) + future_calls > self.max_calls or self.reserved_tokens + total + future_calls * 1000 > self.max_tokens:
            raise ExpectedServiceError('llm_budget_exceeded', 'plano e etapas seguintes não cabem nos tetos')
        available = self.deadline_seconds - (time.monotonic() - self.started)
        seconds_required = total * 60 / self.tpm_limit if self.tpm_limit else 0
        if seconds_required + (len(budgets) + future_calls) * 2 >= available:
            raise ExpectedServiceError('llm_deadline', 'plano excede prazo restante')
        self.plans.append({'stage':stage, 'requests':len(budgets), 'future_min_calls':future_calls,
            'reserved_stage_tokens':total, 'retry_reserve':0, 'model':self.model_for(stage),
            'counter':self.counter.mode, 'estimated_stage_seconds':seconds_required,
            'conservative_stage_cost_usd':cost, 'budgets':[b.as_dict() for b in budgets],
            'repair_policy':'explicit_targeted_repair_replanned_within_original_limits'})

    def call(self, stage, system, data, schema, output_cap=None):
        self.require_free_quota_evidence()
        if datetime.now(timezone.utc).date().isoformat() > GEMINI_PRICE_VALID_UNTIL:
            raise ExpectedServiceError('llm_price_unknown', 'preços Gemini precisam de revalidação antes da inferência')
        payload = self.payload(stage, system, data, schema, output_cap)
        budget = self.budget(stage, system, data, schema, output_cap)
        if not budget.fits:
            raise ExpectedServiceError('llm_request_budget', 'pedido Gemini excede limite antes do transporte')
        with self.rate_gate.lease(self.started + self.deadline_seconds):
            if self.calls >= self.max_calls or self.reserved_tokens + budget.total > self.max_tokens:
                raise ExpectedServiceError('llm_budget_exceeded', 'orçamento de chamadas ou tokens esgotado')
            remaining = self.deadline_seconds - (time.monotonic() - self.started)
            if remaining <= 1:
                raise ExpectedServiceError('llm_deadline', 'prazo global esgotado')
            cost = self.nominal_cost(input_tokens=budget.input_tokens, output_tokens=budget.output_tokens)
            if self.committed_cost() + cost > self.nominal_execution_cap_usd:
                raise ExpectedServiceError('llm_execution_nominal_cap', 'teto nominal agregado de USD 0,15 atingido')
            if stage in self.stage_cost_limits and self.committed_cost(stage) + cost > self.stage_cost_limits[stage]:
                raise ExpectedServiceError('llm_stage_cost_budget', 'teto nominal da etapa atingido')
            diagnostic = {'stage': stage, 'provider': self.provider, 'model': self.model_for(stage),
                'prompt_version': self.prompt_version,
                'reasoning_effort': self.resolved_identity['reasoning_effort'],
                'attempt': self.calls + 1, **budget.as_dict(), 'counter': self.counter.mode,
                'usage_known': False, 'status': 'started',
                'payload_sha256': sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
            ledger_id = self.quota_ledger.reserve_attempt(model=self.model, job_id=self.quota_job_id,
                payload_sha256=diagnostic['payload_sha256'], tokens=budget.total)
            self.calls += 1
            self.reserved_tokens += budget.total
            self.attempt_records.append(diagnostic)
            started = time.monotonic()
            # A full-source structured review can outlast the legacy 25-second
            # Groq timeout. Keep it inside the unchanged execution deadline.
            transport_timeout = min(remaining, 300)
            diagnostic['transport_timeout_seconds'] = transport_timeout
            try:
                raw = getattr(self, 'operational_transport', self.transport)(payload, self.api_key, transport_timeout)
            except HTTPError as exc:
                diagnostic.update(status='http_error', http_status=exc.code)
                self.quota_ledger.settle_attempt(ledger_id, actual_tokens=None, status='unknown', http_status=exc.code)
                try:
                    error = json.load(exc).get('error', {})
                except (ValueError, TypeError, OSError, AttributeError):
                    error = {}
                headers = exc.headers or {}
                # Keep allowlisted metadata only, never arbitrary provider error text.
                observed = {name: str(headers.get(name))[:100] for name in ('Retry-After',
                    'x-ratelimit-limit-tokens', 'x-ratelimit-remaining-tokens', 'x-ratelimit-reset-tokens',
                    'x-ratelimit-limit-requests', 'x-ratelimit-remaining-requests', 'x-ratelimit-reset-requests')
                    if headers.get(name) is not None}
                delay = _retry_seconds(headers, error)
                diagnostic.update(rate_limit_headers=observed, retry_after_seconds=delay)
                self.quota_ledger.observe(self.model, {'http_status': exc.code, 'headers': observed,
                    'retry_after_seconds': delay}, retry_seconds=delay, rate_limited=exc.code == 429)
                details = error.get('details', []) if isinstance(error, dict) else []
                invalid_key = any(isinstance(detail, dict) and detail.get('reason') == 'API_KEY_INVALID'
                    for detail in (details if isinstance(details, list) else []))
                invalid_schema = (exc.code == 400 and isinstance(error, dict)
                    and 'schema' in str(error.get('message', '')).lower())
                code = ('gemini_rate_limit' if exc.code == 429 else 'gemini_auth' if exc.code in (401, 403) or invalid_key
                    else 'gemini_model_unavailable' if exc.code == 404 else 'gemini_schema_invalid' if invalid_schema
                    else 'gemini_request_invalid' if exc.code == 400 else 'gemini_unavailable' if exc.code == 503
                    else 'gemini_http_error')
                message = ('Gemini está temporariamente indisponível/sobrecarregado; tentativa não repetida'
                    if exc.code == 503 else f'Gemini retornou HTTP {exc.code}; tentativa não repetida')
                raise ExpectedServiceError(code, message) from exc
            except (URLError, TimeoutError, OSError) as exc:
                diagnostic['status'] = 'unavailable'
                self.quota_ledger.settle_attempt(ledger_id, actual_tokens=None, status='unknown')
                raise ExpectedServiceError('gemini_unavailable', 'falha de rede/DNS ou timeout Gemini; consumo desconhecido') from exc
            except (ValueError, TypeError) as exc:
                diagnostic['status'] = 'invalid_response'
                self.quota_ledger.settle_attempt(ledger_id, actual_tokens=None, status='unknown')
                raise ExpectedServiceError('invalid_llm_response', 'resposta HTTP Gemini não contém JSON válido') from exc
            finally:
                diagnostic['latency_ms'] = round((time.monotonic() - started) * 1000)
            usage = normalized_usage(raw)
            diagnostic.update(usage=usage, **{k: usage[k] for k in ('usage_known', 'prompt_tokens', 'completion_tokens', 'total_tokens')})
            self.quota_ledger.observe(self.model, {'rate_limits': raw.get('_rate_limits')} if isinstance(raw, dict) else {})
            actual = usage['total_tokens']
            self.quota_ledger.settle_attempt(ledger_id, actual_tokens=actual, status='reported' if usage['usage_known'] else 'unknown')
            if usage['usage_known']:
                self.prompt_tokens += usage['prompt_tokens']
                self.completion_tokens += usage['completion_tokens']
                self.reserved_tokens += actual - budget.total
                self.records.append({'stage': stage, 'model': self.model, 'prompt_version': self.prompt_version,
                    'latency_ms': diagnostic['latency_ms'], 'prompt_tokens': usage['prompt_tokens'],
                    'completion_tokens': usage['completion_tokens'], 'at': datetime.now(timezone.utc).isoformat()})
                if usage['prompt_tokens'] > budget.input_tokens or usage['completion_tokens'] > budget.output_tokens:
                    diagnostic['status'] = 'estimate_underflow'
                    raise ExpectedServiceError('token_estimate_underflow', 'uso Gemini excedeu estimativa; replanejamento necessário')
            try:
                choice = raw['choices'][0]
                diagnostic['finish_reason'] = choice.get('finish_reason')
                if choice['message'].get('refusal'):
                    diagnostic['status'] = 'refused'
                    raise ExpectedServiceError('llm_refusal', 'Gemini recusou a solicitação')
                if choice.get('finish_reason') != 'stop':
                    diagnostic['status'] = 'truncated'
                    raise ExpectedServiceError('llm_truncated', 'resposta Gemini incompleta ou interrompida')
                value = parse_json(choice['message']['content'])
                validate_json(value, schema)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                diagnostic['status'] = 'invalid_response'
                raise ExpectedServiceError('invalid_llm_response', 'resposta Gemini fora do schema local') from exc
            diagnostic['status'] = 'complete'
            return value


def make_session(api_key=None, *, transport=_default_transport):
    identity = configuration()
    key = api_key if api_key is not None else environment_value('GEMINI_API_KEY')
    if not key.strip():
        raise ExpectedServiceError('gemini_key_missing', 'GEMINI_API_KEY ausente; preencha apenas o .env local ou o ambiente')
    return GeminiSession(api_key=key, model=identity['models']['extract'], transport=transport,
        free_only=identity['free_only'], limits=configured_limits(),
        account_scope=identity['provider_limits']['account_scope'])
