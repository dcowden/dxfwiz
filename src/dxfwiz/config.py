from __future__ import annotations

import logging
from pathlib import Path

from pydantic import Field
from typing import Literal

from dxfwiz.schemas.common import StrictModel
from dxfwiz.yaml_io import load_yaml_file


logger = logging.getLogger(__name__)


class GeminiConfig(StrictModel):
    api_key: str = ""
    model: str = "gemini/gemini-2.5-pro"
    token_limit: int = Field(default=1_000_000, gt=0)
    temperature: float = Field(default=0.1, ge=0, le=2)
    timeout_seconds: int = Field(default=300, gt=0)
    max_retries: int = Field(default=2, ge=0)


class PlannerConfig(StrictModel):
    mode: Literal["local", "ai"] = "local"


class AppConfig(StrictModel):
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    path = Path(path)
    if not path.exists():
        logger.warning("Config file %s not found; using defaults", path)
        return AppConfig()
    return AppConfig.model_validate(load_yaml_file(path))
