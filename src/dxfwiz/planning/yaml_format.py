from __future__ import annotations

from copy import deepcopy
from io import StringIO
from typing import Any

from ruamel.yaml import YAML


def dump_operation_yaml(data: dict[str, Any]) -> str:
    buffer = StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 120
    yaml.dump(_round_numeric_values(data), buffer)
    return buffer.getvalue()


def _round_numeric_values(data: dict[str, Any]) -> dict[str, Any]:
    units = data.get("units", {})
    length_units = units.get("length") if isinstance(units, dict) else None
    precision = 2 if length_units == "mm" else 3
    return _round_value(deepcopy(data), precision)


def _round_value(value: Any, precision: int) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, precision)
    if isinstance(value, list):
        return [_round_value(item, precision) for item in value]
    if isinstance(value, dict):
        return {key: _round_value(item, precision) for key, item in value.items()}
    return value
