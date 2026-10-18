"""Contagem offline, admissibilidade e cota compartilhada; nunca corta a fonte."""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from hashlib import sha256
from functools import lru_cache
import base64
import json
import math
from pathlib import Path
import re
from threading import RLock
import time

from suno_asset.services.protocols import ExpectedServiceError

# Leave a small guard beyond the published rolling-minute window. Our ledger
# timestamps admission, while the provider may account the final generated
# token later; the margin avoids crossing an 8k TPM boundary at the edge.
RATE_WINDOW_COOLDOWN_SECONDS = 65.1

TOKENIZER_URL = 'https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken'
TOKENIZER_SHA256 = '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d'
TOKENIZER_PATH = Path('data/tokenizers/o200k_base.tiktoken')
# o200k_base ordinary text uses the same BPE as GPT-OSS/o200k_harmony.
# Pattern from OpenAI tiktoken (MIT), tiktoken_ext/openai_public.py.
PATTERN = '|'.join([
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r'\p{N}{1,3}', r' ?[^\s\p{L}\p{N}]+[\r\n/]*', r'\s*[\r\n]+', r'\s+(?!\S)', r'\s+',
])


@lru_cache(maxsize=2)
def _verified_encoding(vocabulary: bytes):
    """Reuse immutable BPE data; each caller verifies the bytes it just read."""
    import tiktoken
    ranks = {base64.b64decode(token): int(rank)
        for token, rank in (line.split() for line in vocabulary.splitlines() if line)}
    return tiktoken.Encoding(name='suno_o200k', pat_str=PATTERN,
        mergeable_ranks=ranks, special_tokens={})


class TokenCounter:
    """No download on import/request. Missing vocabulary uses a byte upper bound."""
    def __init__(self, path: Path | None = TOKENIZER_PATH):
        self.encoding = None
        self.mode = 'utf8_upper_bound'
        if path is not None and path.is_file():
            vocabulary = path.read_bytes()
            if sha256(vocabulary).hexdigest() != TOKENIZER_SHA256:
                raise ExpectedServiceError('tokenizer_invalid', 'vocabulário local com hash divergente')
            self.encoding = _verified_encoding(vocabulary)
            self.mode = 'o200k_bpe_with_margin'

    def count(self, text: str) -> int:
        return len(self.encoding.encode_ordinary(text)) if self.encoding else len(text.encode('utf-8'))

    def estimate(self, payload: dict) -> int:
        # Count the whole serialized request, including schema. Server-side chat
        # wrapping/schema treatment is unknown: 15% + 128 tokens covers overhead
        # conservatively, and actual usage is checked after every success.
        serialized = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        return math.ceil(self.count(serialized) * 1.15) + 128


@dataclass(frozen=True)
class RequestBudget:
    input_tokens: int
    output_tokens: int
    token_limit: int
    context_limit: int
    safety_fraction: float = 0.90

    @property
    def total(self): return self.input_tokens + self.output_tokens

    @property
    def fits(self):
        return self.total <= min(int(self.token_limit * self.safety_fraction), self.context_limit)

    def as_dict(self):
        return {'input_estimate': self.input_tokens, 'output_reserved': self.output_tokens,
            'total_reserved': self.total, 'tpm_limit': self.token_limit,
            'admissible_tokens': min(int(self.token_limit * self.safety_fraction), self.context_limit),
            'fits': self.fits}


def seconds(value: str | None) -> float | None:
    if not value: return None
    parts = re.findall(r'([0-9]+(?:\.[0-9]+)?)(ms|h|m|s)', str(value))
    if not parts or ''.join(number + unit for number, unit in parts) != value: return None
    return sum(float(number) * {'ms': .001, 's': 1, 'm': 60, 'h': 3600}[unit] for number, unit in parts)


@dataclass
class RateGate:
    """Serializes reservations/transport in one process; no distributed guarantee.

    Local 60-second reservations hold full output caps while usage is unknown.
    Once a response reports valid usage, the reservation is settled to the
    reported token count. Headers can only reduce local availability.
    """
    lock: RLock = field(default_factory=RLock)
    reservations: list[tuple[float, int]] = field(default_factory=list)
    daily_reservations: list[tuple[float, int]] = field(default_factory=list)
    remaining: int | None = None
    reset_at: float | None = None
    observed_limit: int | None = None
    enforce_window: bool = True

    @contextmanager
    def lease(self, deadline: float):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                raise ExpectedServiceError('llm_deadline', 'prazo esgotado aguardando controle de cota')
            if self.lock.acquire(timeout=min(.25, remaining - 1)): break
        try: yield
        finally: self.lock.release()

    def delay(self, tokens: int, limit: int, now: float, rpm_limit: int | None = None) -> float:
        self.reservations[:] = [(at, n) for at, n in self.reservations
            if now - at < RATE_WINDOW_COOLDOWN_SECONDS]
        # Each request is admitted with a 90% reserve. Keep that same margin
        # across the rolling window for the organization-wide token ceiling.
        aggregate_limit = int(limit * 0.9)
        available = aggregate_limit - sum(n for _, n in self.reservations)
        local = max(0.0, self.reservations[0][0] + RATE_WINDOW_COOLDOWN_SECONDS - now) if self.enforce_window and available < tokens and self.reservations else 0
        remote = max(0.0, self.reset_at - now) if (
            self.remaining is not None and self.remaining < tokens and self.reset_at is not None) else 0
        request_delay = max(0.0, self.reservations[0][0] + RATE_WINDOW_COOLDOWN_SECONDS - now) if (
            self.enforce_window and rpm_limit is not None and len(self.reservations) >= rpm_limit) else 0
        return max(local, remote, request_delay)

    def check_daily(self, tokens: int, now: float, requests_limit: int | None,
                    tokens_limit: int | None):
        # A rolling local guard is conservative accounting, not evidence about
        # the provider's reset timezone or consumption by other processes.
        self.daily_reservations[:] = [(at, n) for at, n in self.daily_reservations if now - at < 86400]
        if ((requests_limit is not None and len(self.daily_reservations) >= requests_limit)
            or (tokens_limit is not None and sum(n for _, n in self.daily_reservations) + tokens > tokens_limit)):
            raise ExpectedServiceError('llm_daily_budget', 'cota diária local configurada esgotada')

    def update(self, headers: dict, now: float):
        try:
            limit = int(headers.get('limit') or 0)
            if limit > 0: self.observed_limit = limit
            self.remaining = int(headers['remaining']) if headers.get('remaining') is not None else None
            reset = seconds(headers.get('reset'))
            self.reset_at = now + reset + .5 if reset is not None else None
        except (ValueError, TypeError):
            self.remaining = self.reset_at = None

    def settle_reservation(self, reserved_at: float, reserved_tokens: int, actual_tokens: int):
        """Replace a known call's reservation without changing its timestamp.

        Keep the original time so the rolling TPM/TPD window remains anchored
        to when the provider call happened. Search from the end in case a test
        clock or coarse timer produced equal timestamps for adjacent calls.
        """
        with self.lock:
            for reservations in (self.reservations, self.daily_reservations):
                for index in range(len(reservations) - 1, -1, -1):
                    at, tokens = reservations[index]
                    if at == reserved_at and tokens == reserved_tokens:
                        reservations[index] = (at, actual_tokens)
                        break


# Conservative default: all keys/models share one organization bucket. A caller
# can provide a separate gate when it can establish independent quota scopes.
SHARED_RATE_GATE = RateGate()
