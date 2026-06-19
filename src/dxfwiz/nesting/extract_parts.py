from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable

import ezdxf
from ezdxf import path

from dxfwiz.dxf import CleanDxfConfig, clean_dxf, write_geometry_yaml


@dataclass(frozen=True)
class ExtractedPart:
    name: str
    path: Path
    entity_ids: tuple[str, ...]
    bounding_box: tuple[float, float, float, float]


@dataclass(frozen=True)
class ExtractPartsResult:
    source_path: Path
    output_dir: Path
    parts: tuple[ExtractedPart, ...]


def extract_parts(
    source_path: str | Path,
    output_dir: str | Path,
    *,
    clean_config: CleanDxfConfig | None = None,
    name_prefix: str = "part",
) -> ExtractPartsResult:
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix="dxfwiz_extract_parts_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        fixed_path = temp_dir / f"{source_path.stem}_fixed.dxf"
        geom_path = temp_dir / f"{source_path.stem}_geom.yaml"
        clean_dxf(source_path, fixed_path, clean_config or CleanDxfConfig())
        geom_data = write_geometry_yaml(
            fixed_path,
            geom_path,
            original_file=source_path.name,
            cleaned_file=fixed_path.name,
        )
        fixed_doc = ezdxf.readfile(fixed_path)
        parts = _write_parts(fixed_doc, geom_data, output_dir, name_prefix)

    _write_manifest(source_path, output_dir, parts)
    return ExtractPartsResult(source_path=source_path, output_dir=output_dir, parts=tuple(parts))


def _write_parts(
    fixed_doc,
    geom_data: dict[str, Any],
    output_dir: Path,
    name_prefix: str,
) -> list[ExtractedPart]:
    entities_by_id = {entity["id"]: entity for entity in geom_data["entities"]}
    dxf_entities_by_handle = {
        str(entity.dxf.handle): entity for entity in fixed_doc.modelspace()
    }
    units = 1 if geom_data.get("units", {}).get("length") == "in" else fixed_doc.units
    scale = _coordinate_scale(entities_by_id.values(), dxf_entities_by_handle)
    part_nodes = _part_nodes(geom_data["entity_map"])
    sorted_part_nodes = sorted(
        part_nodes,
        key=lambda node: (
            entities_by_id[node["entity"]]["bounding_box"]["min"]["y"],
            entities_by_id[node["entity"]]["bounding_box"]["min"]["x"],
        ),
    )

    parts: list[ExtractedPart] = []
    for index, node in enumerate(sorted_part_nodes, start=1):
        entity_ids = tuple(_collect_entity_ids(node))
        bounds = _combined_bounds(entities_by_id[entity_id] for entity_id in entity_ids)
        min_x, min_y, max_x, max_y = bounds
        part_name = f"{name_prefix}_{index:03d}"
        part_path = output_dir / f"{part_name}.dxf"
        part_doc = ezdxf.new("R2000")
        part_doc.units = units
        part_msp = part_doc.modelspace()
        raw_min_x, raw_min_y, _raw_max_x, _raw_max_y = _combined_raw_bounds(
            _source_entity(entities_by_id[entity_id], dxf_entities_by_handle)
            for entity_id in entity_ids
        )
        for entity_id in entity_ids:
            source_entity = _source_entity(entities_by_id[entity_id], dxf_entities_by_handle)
            copied = source_entity.copy()
            copied.translate(-raw_min_x, -raw_min_y, 0.0)
            if scale != 1.0:
                copied.scale(scale, scale, scale)
            copied.dxf.discard("handle")
            part_msp.add_entity(copied)
        part_doc.saveas(part_path)
        parts.append(
            ExtractedPart(
                name=part_name,
                path=part_path,
                entity_ids=entity_ids,
                bounding_box=(min_x, min_y, max_x, max_y),
            )
        )
    return parts


def _part_nodes(nodes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for node in nodes:
        if node["role"] == "part":
            parts.append(node)
            continue
        parts.extend(_part_nodes(node.get("children", [])))
    return parts


def _collect_entity_ids(node: dict[str, Any]) -> list[str]:
    entity_ids = [node["entity"]]
    for child in node.get("children", []):
        entity_ids.extend(_collect_entity_ids(child))
    return entity_ids


def _combined_bounds(entities: Iterable[dict[str, Any]]) -> tuple[float, float, float, float]:
    entity_list = list(entities)
    min_x = min(entity["bounding_box"]["min"]["x"] for entity in entity_list)
    min_y = min(entity["bounding_box"]["min"]["y"] for entity in entity_list)
    max_x = max(entity["bounding_box"]["max"]["x"] for entity in entity_list)
    max_y = max(entity["bounding_box"]["max"]["y"] for entity in entity_list)
    return min_x, min_y, max_x, max_y


def _source_entity(entity: dict[str, Any], dxf_entities_by_handle: dict[str, Any]) -> Any:
    for source_ref in entity["source_refs"]:
        if source_ref["kind"] == "dxf_handle":
            return dxf_entities_by_handle[source_ref["value"]]
    raise ValueError(f"No DXF handle found for geometry entity {entity['id']}")


def _coordinate_scale(
    entities: Iterable[dict[str, Any]],
    dxf_entities_by_handle: dict[str, Any],
) -> float:
    for entity in entities:
        source_entity = _source_entity(entity, dxf_entities_by_handle)
        raw_bounds = _entity_raw_bounds(source_entity)
        if raw_bounds is None:
            continue
        raw_width = raw_bounds[2] - raw_bounds[0]
        raw_height = raw_bounds[3] - raw_bounds[1]
        geom_box = entity["bounding_box"]
        geom_width = geom_box["max"]["x"] - geom_box["min"]["x"]
        geom_height = geom_box["max"]["y"] - geom_box["min"]["y"]
        if raw_width > 1e-9 and geom_width > 1e-9:
            return geom_width / raw_width
        if raw_height > 1e-9 and geom_height > 1e-9:
            return geom_height / raw_height
    return 1.0


def _combined_raw_bounds(entities: Iterable[Any]) -> tuple[float, float, float, float]:
    bounds = [_entity_raw_bounds(entity) for entity in entities]
    bounds = [bound for bound in bounds if bound is not None]
    if not bounds:
        raise ValueError("Cannot compute raw bounds for empty part")
    return (
        min(bound[0] for bound in bounds),
        min(bound[1] for bound in bounds),
        max(bound[2] for bound in bounds),
        max(bound[3] for bound in bounds),
    )


def _entity_raw_bounds(entity) -> tuple[float, float, float, float] | None:
    if entity.dxftype() == "CIRCLE":
        center = entity.dxf.center
        radius = float(entity.dxf.radius)
        return (
            float(center.x) - radius,
            float(center.y) - radius,
            float(center.x) + radius,
            float(center.y) + radius,
        )
    if entity.dxftype() == "LWPOLYLINE":
        points = [
            (float(vertex.x), float(vertex.y))
            for vertex in path.make_path(entity).flattening(0.01)
        ]
        if not points:
            return None
        return (
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        )
    return None


def _write_manifest(source_path: Path, output_dir: Path, parts: list[ExtractedPart]) -> None:
    manifest = {
        "source": source_path.name,
        "part_count": len(parts),
        "parts": [
            {
                "name": part.name,
                "file": part.path.name,
                "entity_ids": list(part.entity_ids),
                "bounding_box": {
                    "min": {"x": part.bounding_box[0], "y": part.bounding_box[1]},
                    "max": {"x": part.bounding_box[2], "y": part.bounding_box[3]},
                },
            }
            for part in parts
        ],
    }
    (output_dir / "parts.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract distinct nested parts from a DXF.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--name-prefix", default="part")
    args = parser.parse_args(argv)

    result = extract_parts(args.source, args.output_dir, name_prefix=args.name_prefix)
    print(f"Wrote {len(result.parts)} part DXF(s) to {result.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
