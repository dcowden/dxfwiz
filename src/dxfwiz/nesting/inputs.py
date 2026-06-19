from __future__ import annotations

from pathlib import Path
from typing import Iterable

from dxfwiz.nesting.engine import PartShape, load_part_shapes
from dxfwiz.nesting.extract_parts import extract_parts


def load_part_shapes_from_dxf_inputs(
    input_paths: Iterable[str | Path],
    work_dir: str | Path,
) -> list[PartShape]:
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    part_paths: list[Path] = []
    for input_path in input_paths:
        input_path = Path(input_path)
        output_dir = work_dir / input_path.stem
        result = extract_parts(
            input_path,
            output_dir,
            name_prefix=input_path.stem,
        )
        part_paths.extend(part.path for part in result.parts)

    return load_part_shapes(part_paths)
