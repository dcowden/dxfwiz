from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


yaml = YAML(typ="safe")


def load_yaml_file(path: str | Path) -> dict[str, Any]:
    data = yaml.load(Path(path))
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping at {path}")
    return data


def dump_yaml_file(path: str | Path, data: dict[str, Any]) -> None:
    writer = YAML()
    writer.default_flow_style = False
    writer.dump(data, Path(path))

