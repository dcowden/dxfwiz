from pathlib import Path

from dxfwiz.schemas import GeometryFile
from dxfwiz.ui.app import render_display
from dxfwiz.ui.service import ProjectService
from dxfwiz.yaml_io import load_yaml_file


ROOT = Path(__file__).resolve().parents[1]


def test_project_service_creates_project_artifacts(tmp_path):
    service = ProjectService(
        workspace_dir=tmp_path,
        machine_yaml=ROOT / "examples" / "machine.yaml",
        planner_yaml=ROOT / "examples" / "planner.yaml",
    )
    source = ROOT / "tests" / "dxf_clean" / "2xintake" / "2xintakev3_and_2xkickerv1.dxf"

    artifacts = service.create_project_from_upload(source.name, source.read_bytes())

    assert artifacts.original_dxf.parent == tmp_path / artifacts.project_id
    assert artifacts.fixed_dxf.exists()
    assert artifacts.geom_yaml.exists()
    assert artifacts.geometry_svg.exists()

    geom = GeometryFile.model_validate(load_yaml_file(artifacts.geom_yaml))
    assert geom.summary.entity_count == 59
    assert geom.entity_map[0].role == "frame"


def test_render_display_includes_machine_and_geometry_layers(tmp_path):
    service = ProjectService(
        workspace_dir=tmp_path,
        machine_yaml=ROOT / "examples" / "machine.yaml",
        planner_yaml=ROOT / "examples" / "planner.yaml",
    )
    source = ROOT / "tests" / "dxf_clean" / "2xintake" / "2xintakev3_and_2xkickerv1.dxf"
    artifacts = service.create_project_from_upload(source.name, source.read_bytes())

    html = render_display(service.load_machine(), artifacts)

    assert "machine-bed" in html
    assert "geometry-layer" in html
    assert "role-frame" in html
    assert "geom: in (guessed)" in html
    assert "machine-x-arrow" in html
    assert "machine-y-arrow" in html
    assert 'title="Zoom in"' in html
    assert 'title="Zoom to extents"' in html
