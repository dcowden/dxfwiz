import logging
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


yaml = YAML(typ="safe")
logger = logging.getLogger(__name__)


def load_yaml_file(path: str | Path) -> dict[str, Any]:
    logger.debug("Loading YAML file %s", path)
    data = yaml.load(Path(path))
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping at {path}")
    return data


def dump_yaml_file(path: str | Path, data: dict[str, Any]) -> None:
    logger.debug("Writing YAML file %s", path)
    writer = YAML()
    writer.default_flow_style = False
    writer.dump(data, Path(path))
