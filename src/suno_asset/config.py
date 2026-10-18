"""Configuração mínima de desenvolvimento sem dependência de segredos."""

from pathlib import Path
import os

from pydantic import BaseModel, ConfigDict, Field
from suno_asset.services.provider_config import environment_value


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    app_name: str = "Suno Asset"
    mode: str = "simulated"
    data_root: Path = Field(default_factory=lambda: Path(os.getenv("SUNO_DATA_ROOT", "data")))
    promoted_configuration_path: Path | None = Field(default_factory=lambda: Path(os.environ['SUNO_M7_PROMOTION']) if os.getenv('SUNO_M7_PROMOTION') else None)
    mvp_release_path: Path | None = Field(default_factory=lambda: Path(environment_value('SUNO_M7_MVP_RELEASE')) if environment_value('SUNO_M7_MVP_RELEASE') else None)

