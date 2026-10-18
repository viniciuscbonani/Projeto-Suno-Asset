"""Cotas explícitas por janela; hipóteses não se tornam limites efetivos."""
from __future__ import annotations

from dataclasses import dataclass
import os

from suno_asset.services.protocols import ExpectedServiceError


@dataclass(frozen=True)
class ProviderLimits:
    tokens_per_minute: int = 8000
    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    tokens_per_day: int | None = None

    def __post_init__(self):
        if type(self.tokens_per_minute) is not int or self.tokens_per_minute < 1000:
            raise ValueError('tokens_per_minute precisa ser inteiro >= 1000')
        for value in (self.requests_per_minute, self.requests_per_day, self.tokens_per_day):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError('Limites opcionais precisam ser inteiros positivos')

    def as_dict(self):
        return {'version': 'groq-limits-v1', 'minute': {
            'tokens': self.tokens_per_minute, 'requests': self.requests_per_minute},
            'day': {'requests': self.requests_per_day, 'tokens': self.tokens_per_day},
            'configuration_evidence': 'operator_configuration_or_conservative_default',
            'provider_day_window': 'unknown', 'local_day_window_seconds': 86400,
            'unverified_hypotheses': {'requests': 900, 'tokens': 240000,
                                     'model': None, 'window': None},
            'retention': 'unknown', 'validation_sources': 'public_copom_only'}


def limits_from_environment():
    names = ('GROQ_TPM_LIMIT', 'GROQ_RPM_LIMIT', 'GROQ_RPD_LIMIT', 'GROQ_TPD_LIMIT')
    values = []
    try:
        for index, name in enumerate(names):
            raw = os.getenv(name)
            values.append(int(raw) if raw else 8000 if index == 0 else None)
        return ProviderLimits(*values)
    except ValueError as exc:
        raise ExpectedServiceError('groq_config_invalid',
            'Cotas GROQ_TPM/RPM/RPD/TPD_LIMIT exigem inteiros positivos (TPM >= 1000)') from exc
