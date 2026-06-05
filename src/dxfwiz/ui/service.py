from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from dxfwiz.dxf import CleanDxfConfig, CleanDxfResult, clean_dxf, write_geometry_yaml
from dxfwiz.schemas import GeometryFile, MachineFile, PlannerFile
from dxfwiz.svg import render_geometry_svg
from dxfwiz.yaml_io import load_yaml_file


@dataclass(frozen=True)
class ProjectArtifacts:
    project_id: str
    original_dxf: Path
    fixed_dxf: Path
    geom_yaml: Path
    geometry_svg: Path
    clean_result: CleanDxfResult
    geometry: dict


class ProjectService:
    """Project-scoped storage and processing boundary for the UI.

    This is intentionally small and synchronous for now. Later, the same methods can
    enqueue work and read artifacts from object storage, Redis, or a database.
    """

    def __init__(
        self,
        workspace_dir: str | Path,
        machine_yaml: str | Path,
        planner_yaml: str | Path,
    ) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.machine_yaml = Path(machine_yaml)
        self.planner_yaml = Path(planner_yaml)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def load_machine(self) -> MachineFile:
        return MachineFile.model_validate(load_yaml_file(self.machine_yaml))

    def load_planner(self) -> PlannerFile:
        return PlannerFile.model_validate(load_yaml_file(self.planner_yaml))

    def create_project_from_upload(self, filename: str, content: bytes) -> ProjectArtifacts:
        project_id = uuid4().hex
        project_dir = self.workspace_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=False)

        source_name = Path(filename).name
        original_dxf = project_dir / source_name
        fixed_dxf = project_dir / f"{Path(source_name).stem}_fixed.dxf"
        geom_yaml = project_dir / "geom.yaml"
        geometry_svg = project_dir / "geometry.svg"

        original_dxf.write_bytes(content)
        clean_result = clean_dxf(
            original_dxf,
            fixed_dxf,
            CleanDxfConfig(
                gap_tolerance=0.005,
                duplicate_tolerance=0.0005,
                min_segment_length=0.001,
            ),
        )
        geometry = write_geometry_yaml(
            fixed_dxf,
            geom_yaml,
            original_file=original_dxf.name,
            cleaned_file=fixed_dxf.name,
        )
        GeometryFile.model_validate(load_yaml_file(geom_yaml))
        render_geometry_svg(geom_yaml, fixed_dxf, geometry_svg)

        return ProjectArtifacts(
            project_id=project_id,
            original_dxf=original_dxf,
            fixed_dxf=fixed_dxf,
            geom_yaml=geom_yaml,
            geometry_svg=geometry_svg,
            clean_result=clean_result,
            geometry=geometry,
        )
