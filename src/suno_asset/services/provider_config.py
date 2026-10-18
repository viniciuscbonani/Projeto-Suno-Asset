"""Provider selection without loading secrets into process-global environment."""
from __future__ import annotations

import os
from pathlib import Path
import re

from suno_asset.services.protocols import ExpectedServiceError


def local_environment(path: Path = Path('.env')) -> dict[str, str]:
    """Small dotenv subset: assignments, quotes, export and inline comments.

    Never evaluate substitutions or modify os.environ. An explicitly empty
    environment variable takes precedence over the local file as well.
    """
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*?)\s*', line)
        if not match:
            continue
        name, value = match.groups()
        if value.startswith(('"', "'")):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or value[end + 1:].strip() and not value[end + 1:].strip().startswith('#'):
                raise ExpectedServiceError('provider_config_invalid', 'atribuição inválida no .env local')
            value = value[1:end]
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
        values[name] = value
    return values


def environment_value(name: str, default: str = '') -> str:
    return os.environ[name] if name in os.environ else local_environment().get(name, default)


def economy_provider() -> str:
    provider = environment_value('SUNO_ECONOMY_PROVIDER', 'gemini').strip().lower()
    if provider not in {'groq', 'gemini'}:
        raise ExpectedServiceError('provider_config_invalid', 'SUNO_ECONOMY_PROVIDER deve ser gemini ou groq')
    return provider


def provider_identity(profile: str) -> dict:
    """Resolved non-secret configuration; usable before a key is present."""
    if profile == 'economy' and economy_provider() == 'gemini':
        from suno_asset.services.gemini import configuration
        return configuration()
    from suno_asset.services.groq import MODEL, STAGE_OUTPUT
    from suno_asset.services.provider_limits import limits_from_environment
    model = os.getenv('GROQ_MODEL', MODEL)
    models = {stage: (model if profile == 'single' else
        'openai/gpt-oss-20b' if profile == 'hybrid' and stage == 'extract' else
        'openai/gpt-oss-20b' if profile == 'economy' and stage in {'extract', 'generate'} else
        'openai/gpt-oss-120b') for stage in STAGE_OUTPUT}
    return {'provider': 'groq', 'models': models, 'reasoning_effort': 'low',
        'provider_limits': limits_from_environment().as_dict()}


def make_economy_session():
    if economy_provider() == 'gemini':
        from suno_asset.services.gemini import make_session
    else:
        from suno_asset.services.groq import make_session
    return make_session()


def repair_model(session, stage: str) -> str:
    resolver = getattr(session, 'repair_model_for', None)
    return resolver(stage) if resolver else 'openai/gpt-oss-120b'
