from __future__ import annotations

import os
import re
from html import escape
from pathlib import Path
from typing import Any

from nicegui import app, ui

from dxfwiz.schemas import MachineFile, OperationInputsFile, PlannerFile
from dxfwiz.ui.service import ProjectArtifacts, ProjectService
from dxfwiz.yaml_io import load_yaml_file


ROOT_DIR = Path(__file__).resolve().parents[3]
WORKSPACE_DIR = ROOT_DIR / "workspace"
EXAMPLES_DIR = ROOT_DIR / "examples"

service = ProjectService(
    workspace_dir=WORKSPACE_DIR,
    machine_yaml=EXAMPLES_DIR / "machine.yaml",
    planner_yaml=EXAMPLES_DIR / "planner.yaml",
)
app.add_static_files("/workspace", WORKSPACE_DIR)


def main() -> None:
    machine = service.load_machine()
    state: dict[str, Any] = {"project": None, "chat": None, "crumbs": None, "inputs": {}}

    ui.add_head_html(
        """
        <style>
          html, body { margin: 0; width: 100%; height: 100%; overflow: hidden; background: #f8fafc; color: #0f172a; }
          .app-shell { width: 100vw; height: 100vh; display: flex; flex-direction: column; }
          .topbar {
            height: 58px; flex: 0 0 58px; border-bottom: 1px solid #dbe3ef; background: #ffffff;
            display: flex; align-items: center; gap: 14px; padding: 0 18px;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
          }
          .brand { font-weight: 700; color: #0f172a; letter-spacing: 0; }
          .nav-link {
            color: #2563eb; font-weight: 650; cursor: pointer; text-transform: none;
            letter-spacing: 0; font-size: 14px;
          }
          .nav-active { color: #0f172a; font-weight: 750; }
          .nav-muted { color: #94a3b8; font-weight: 650; }
          .step-separator { color: #cbd5e1; font-weight: 700; }
          .content-host { flex: 1; min-height: 0; width: 100%; overflow: hidden; }
          .empty-state {
            width: 100%; height: 100%; display: grid; place-items: center;
            background:
              linear-gradient(90deg, rgba(148,163,184,0.13) 1px, transparent 1px),
              linear-gradient(0deg, rgba(148,163,184,0.13) 1px, transparent 1px),
              #f8fafc;
            background-size: 32px 32px;
          }
          .empty-card {
            width: min(520px, calc(100vw - 40px)); border: 1px solid #dbe3ef;
            background: rgba(255,255,255,0.96); border-radius: 8px; padding: 28px;
            box-shadow: 0 18px 50px rgba(15, 23, 42, 0.08);
          }
          .upload-cta .q-uploader {
            width: 100%; border-radius: 7px; box-shadow: none;
          }
          .upload-cta .q-uploader__header {
            min-height: 58px; background: #1d4ed8;
          }
          .hidden-uploader {
            width: 0; height: 0; overflow: hidden; opacity: 0; position: absolute;
          }
          .primary-cta {
            width: 100%; height: 56px; border-radius: 7px; font-weight: 700;
            box-shadow: 0 10px 24px rgba(37, 99, 235, 0.20);
          }
          .main-grid {
            height: 100%; min-height: 0; display: grid; grid-template-columns: minmax(0, 1fr) 380px;
          }
          .display-card { min-height: 0; padding: 8px; background: #eef3f8; }
          .planning-panel {
            border-left: 1px solid #dbe3ef; background: #ffffff; min-height: 0; height: 100%;
            display: flex; flex-direction: column;
          }
          .viewer-host { height: 100%; min-height: 0; }
          .planning-scroll { flex: 1; min-height: 0; overflow: auto; padding: 14px; }
          .planning-footer { border-top: 1px solid #e2e8f0; padding: 12px; background: #ffffff; }
          .summary-grid { display: grid; grid-template-columns: 1fr auto; gap: 8px 14px; font-size: 13px; }
          .summary-key { color: #64748b; }
          .summary-value { color: #0f172a; font-weight: 650; text-align: right; }
          .input-card {
            border: 1px solid #dbe3ef; border-radius: 7px; background: #f8fafc;
            padding: 10px; margin-bottom: 10px;
          }
          .input-card-title { font-weight: 750; color: #0f172a; font-size: 13px; }
          .input-card-source { color: #64748b; font-size: 12px; margin-top: 2px; margin-bottom: 8px; }
          .input-card.missing { border-color: #fca5a5; background: #fff7f7; }
          .input-card.missing .input-card-title { color: #b91c1c; }
          .missing-note { color: #b91c1c; font-size: 12px; font-weight: 650; margin-bottom: 8px; }
          .input-card .q-field { font-size: 13px; }
          .field-row { display: grid; grid-template-columns: minmax(0, 1fr) 88px; gap: 8px; }
          .yaml-view {
            background: #0f172a; color: #e2e8f0; border-radius: 7px; padding: 14px;
            height: calc(100vh - 180px); overflow: auto; white-space: pre; font: 12px Consolas, monospace;
          }
          .settings-screen { height: 100%; padding: 18px; overflow: hidden; background: #eef3f8; }
          .settings-card {
            height: 100%; background: #ffffff; border: 1px solid #dbe3ef; border-radius: 8px;
            padding: 16px; display: flex; flex-direction: column; box-shadow: 0 16px 40px rgba(15, 23, 42, 0.08);
          }
        </style>
        """
    )
    ui.add_body_html(_viewer_script())

    with ui.element("div").classes("app-shell"):
        with ui.element("div").classes("topbar"):
            ui.label("DXF Wizard").classes("brand")
            with ui.row().classes("items-center gap-2") as crumbs:
                state["crumbs"] = crumbs
            ui.space()
            ui.button(
                "Settings",
                icon="settings",
                on_click=lambda: render_settings_state(state["content"], state),
            ).props("flat dense").classes("nav-link")

        with ui.element("div").classes("content-host") as content:
            state["content"] = content

    render_initial_state(content, state, machine)

    ui.run(title="DXF Wizard", reload=False, port=int(os.environ.get("DXFWIZ_PORT", "8080")))


def render_initial_state(content, state: dict, machine: MachineFile) -> None:
    state["project"] = None
    state["chat"] = None
    state["inputs"] = {}
    _render_crumbs(state, active="choose")
    content.clear()
    with content:
        with ui.element("div").classes("empty-state"):
            with ui.element("div").classes("empty-card"):
                ui.icon("upload_file").classes("text-5xl text-blue-700 mb-3")
                ui.label("Choose a DXF to begin").classes("text-2xl font-semibold text-slate-900")
                ui.label(
                    "DXF Wizard will clean the file, generate geom.yaml, and show the recognized geometry on your machine work area."
                ).classes("text-sm text-slate-600 mt-2 mb-5")
                upload = ui.upload(
                    label="",
                    auto_upload=True,
                    on_upload=lambda event: handle_upload(event, state, machine, content),
                ).props("accept=.dxf,.DXF")
                upload.classes("hidden-uploader")
                ui.button(
                    "Choose a DXF",
                    icon="upload_file",
                    on_click=lambda: upload.run_method("pickFiles"),
                ).props("color=primary unelevated").classes("primary-cta")


def render_project_state(content, state: dict, machine: MachineFile, artifacts: ProjectArtifacts) -> None:
    planner = service.load_planner()
    operation_inputs = OperationInputsFile.model_validate(
        load_yaml_file(EXAMPLES_DIR / "operation_inputs.yaml")
    )
    planning_context = _planning_context(machine, planner, operation_inputs, artifacts)
    _initialize_planning_inputs(state, planning_context)
    _render_crumbs(state, active="plan")
    content.clear()
    with content:
        with ui.element("div").classes("main-grid"):
            with ui.element("div").classes("display-card"):
                ui.html(render_job_display(artifacts)).classes("viewer-host w-full")
            with ui.element("div").classes("planning-panel"):
                ui.label("Planning").classes("px-4 pt-4 text-lg font-semibold text-slate-900")
                with ui.element("div").classes("planning-scroll"):
                    with ui.expansion("Geometry Summary", icon="analytics", value=True).classes("w-full"):
                        _render_geometry_summary(planning_context)
                    with ui.expansion("Operation Inputs", icon="fact_check", value=True).classes("w-full mt-2"):
                        input_widgets = _render_operation_input_cards(planning_context, state)
                with ui.element("div").classes("planning-footer"):
                    intent_input = ui.textarea(
                        label="Planning notes",
                        placeholder="additional instructions",
                        value=state["inputs"].get("planning_notes", ""),
                    ).props("outlined dense rows=3").classes("w-full")
                    generate_button = ui.button(
                        "Generate plan",
                        icon="play_arrow",
                    ).props("color=primary unelevated").classes("w-full mt-2")

                    generate_state = {"ready": False}

                    def update_generate_state() -> None:
                        _capture_input_values(state, input_widgets, intent_input)
                        missing = [
                            widget["label"]
                            for widget in input_widgets
                            if widget["required"] and not _widget_has_value(widget["control"])
                        ]
                        if missing:
                            generate_state["ready"] = False
                            generate_button.disable()
                        else:
                            generate_state["ready"] = True
                            generate_button.enable()

                    def generate_plan() -> None:
                        if not generate_state["ready"]:
                            return
                        state["inputs"]["planning_notes"] = intent_input.value or ""
                        note = intent_input.value or ""
                        suffix = f" Notes captured: {note}" if note else ""
                        ui.notify(
                            "Required inputs are present. Operation-plan generation is the next implementation step."
                            + suffix,
                            type="positive",
                        )

                    for widget in input_widgets:
                        for control in _iter_controls(widget["control"]):
                            control.on("update:model-value", lambda _: update_generate_state())
                    intent_input.on("update:model-value", lambda _: update_generate_state())
                    generate_button.on("click", generate_plan)
                    update_generate_state()


def render_settings_state(content, state: dict) -> None:
    _render_crumbs(state, active="settings")
    content.clear()
    with content:
        with ui.element("div").classes("settings-screen"):
            with ui.element("div").classes("settings-card"):
                ui.label("Settings").classes("text-xl font-semibold text-slate-900")
                with ui.tabs().classes("w-full mt-2") as tabs:
                    machine_tab = ui.tab("machine.yaml", icon="precision_manufacturing")
                    planner_tab = ui.tab("planner.yaml", icon="rule")
                with ui.tab_panels(tabs, value=machine_tab).classes("w-full grow min-h-0"):
                    with ui.tab_panel(machine_tab).classes("h-full min-h-0"):
                        ui.html(_yaml_pre(EXAMPLES_DIR / "machine.yaml"))
                    with ui.tab_panel(planner_tab).classes("h-full min-h-0"):
                        ui.html(_yaml_pre(EXAMPLES_DIR / "planner.yaml"))


def _planning_context(
    machine: MachineFile,
    planner: PlannerFile,
    operation_inputs: OperationInputsFile,
    artifacts: ProjectArtifacts,
) -> dict[str, Any]:
    known: dict[str, str] = {}
    sources: dict[str, str] = {}
    stock_size = _stock_size_from_geometry(artifacts.geometry)
    if stock_size:
        known["stock_size"] = stock_size
        sources["stock_size"] = "geom.yaml frame"
    defaults = planner.defaults
    if defaults.stock is not None:
        known["stock_thickness"] = f"{defaults.stock.thickness:g}"
        sources["stock_thickness"] = "planner.yaml default"
        known["stock_material"] = defaults.stock.material
        sources["stock_material"] = "planner.yaml default"
        known["z_zero_position"] = defaults.stock.z_zero
        sources["z_zero_position"] = "planner.yaml default"
    if defaults.workholding:
        known["workholding_method"] = ", ".join(defaults.workholding)
        sources["workholding_method"] = "planner.yaml default"
    if defaults.coordinate_system:
        known["coordinate_system"] = defaults.coordinate_system
        sources["coordinate_system"] = "planner.yaml default"

    missing = [
        {"name": item.name, "description": item.description}
        for item in operation_inputs.inputs
        if item.required and item.name not in known
    ]

    return {
        "machine": machine,
        "planner": planner,
        "artifacts": artifacts,
        "known": known,
        "sources": sources,
        "missing": missing,
        "operation_inputs": operation_inputs,
    }


def _render_geometry_summary(context: dict[str, Any]) -> None:
    geometry = context["artifacts"].geometry
    summary = geometry["summary"]
    units = geometry["units"]
    box = summary["bounding_box"]
    width = box["max"]["x"] - box["min"]["x"]
    height = box["max"]["y"] - box["min"]["y"]
    rows = [
        ("Units", f"{units['length']} ({units['source']})"),
        ("Entity count", str(summary["entity_count"])),
        ("Closed loops", str(summary["closed_count"])),
        ("Open paths", str(summary["open_count"])),
        ("Ignored", str(summary["ignored_count"])),
        ("Extents", f"{width:.3f} x {height:.3f} {units['length']}"),
    ]
    with ui.element("div").classes("summary-grid"):
        for key, value in rows:
            ui.label(key).classes("summary-key")
            ui.label(value).classes("summary-value")


def _initialize_planning_inputs(state: dict[str, Any], context: dict[str, Any]) -> None:
    inputs = state.setdefault("inputs", {})
    known = context["known"]
    geometry = context["artifacts"].geometry
    planner: PlannerFile = context["planner"]
    defaults = planner.defaults
    stock = defaults.stock
    inputs.setdefault("stock_xy", known.get("stock_size", ""))
    inputs.setdefault("stock_units", geometry["units"]["length"])
    inputs.setdefault("stock_thickness", known.get("stock_thickness", ""))
    inputs.setdefault("stock_material", known.get("stock_material", ""))
    inputs.setdefault("z_zero_position", known.get("z_zero_position", ""))
    inputs.setdefault("coordinate_system", known.get("coordinate_system", ""))
    inputs.setdefault("workholding_method", [item.strip() for item in known.get("workholding_method", "").split(",") if item.strip()])
    inputs.setdefault("tools", defaults.default_tool or "")
    inputs.setdefault("planning_notes", "")
    if stock is not None and not inputs.get("stock_thickness"):
        inputs["stock_thickness"] = f"{stock.thickness:g}"


def _render_operation_input_cards(context: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    machine: MachineFile = context["machine"]
    planner: PlannerFile = context["planner"]
    geometry = context["artifacts"].geometry
    sources: dict[str, str] = context["sources"]
    inputs = state["inputs"]
    widgets: list[dict[str, Any]] = []

    stock_missing = not inputs.get("stock_xy") or not inputs.get("stock_material")
    with ui.element("div").classes("input-card" + (" missing" if stock_missing else "")):
        ui.label("Stock").classes("input-card-title")
        ui.label(_sources_text(["stock_size", "stock_material", "stock_thickness"], sources)).classes("input-card-source")
        if stock_missing:
            ui.label("Missing required stock information.").classes("missing-note")
        with ui.element("div").classes("field-row"):
            stock_xy = ui.input("XY size", value=inputs.get("stock_xy", "")).props("outlined dense")
            stock_units = ui.select(["in", "mm"], value=inputs.get("stock_units") or geometry["units"]["length"], label="UOM").props("outlined dense")
        stock_thickness = ui.number(
            "Thickness",
            value=_float_or_none(inputs.get("stock_thickness")),
            format="%.4f",
        ).props("outlined dense").classes("w-full")
        stock_material = ui.select(
            ["plywood", "polycarbonate"],
            value=inputs.get("stock_material") or None,
            label="Material",
        ).props("outlined dense").classes("w-full")
    widgets.extend(
        [
            {"name": "stock_size", "label": "Stock", "required": True, "control": stock_xy, "state_key": "stock_xy"},
            {"name": "stock_material", "label": "Stock material", "required": True, "control": stock_material, "state_key": "stock_material"},
            {"name": "stock_units", "label": "Stock units", "required": False, "control": stock_units, "state_key": "stock_units"},
            {"name": "stock_thickness", "label": "Stock thickness", "required": False, "control": stock_thickness, "state_key": "stock_thickness"},
        ]
    )

    coordinate_missing = not inputs.get("z_zero_position") or not inputs.get("coordinate_system")
    with ui.element("div").classes("input-card" + (" missing" if coordinate_missing else "")):
        ui.label("Coordinate System").classes("input-card-title")
        ui.label(_sources_text(["z_zero_position", "coordinate_system"], sources)).classes("input-card-source")
        if coordinate_missing:
            ui.label("Missing required coordinate system information.").classes("missing-note")
        z_zero = ui.select(
            ["stock_top", "spoilboard_top"],
            value=inputs.get("z_zero_position") or None,
            label="Z zero",
        ).props("outlined dense").classes("w-full")
        coordinate_system = ui.select(
            ["G54", "G55", "G56", "G57", "G58", "G59"],
            value=inputs.get("coordinate_system") or None,
            label="Coordinate system",
        ).props("outlined dense").classes("w-full")
    widgets.extend(
        [
            {"name": "z_zero_position", "label": "Z zero", "required": True, "control": z_zero, "state_key": "z_zero_position"},
            {"name": "coordinate_system", "label": "Coordinate system", "required": True, "control": coordinate_system, "state_key": "coordinate_system"},
        ]
    )

    with ui.element("div").classes("input-card"):
        ui.label("Tools").classes("input-card-title")
        ui.label("Planner preference | source: planner.yaml and machine.yaml").classes("input-card-source")
        tool_options = {tool.id: f"{tool.id} - {tool.description}" for tool in machine.tools}
        tool_control = ui.select(
            options=tool_options,
            value=inputs.get("tools") or planner.defaults.default_tool,
            label="Tool",
        ).props("outlined dense").classes("w-full")
    widgets.append({"name": "tools", "label": "Tools", "required": False, "control": tool_control, "state_key": "tools"})

    workholding_missing = not inputs.get("workholding_method")
    with ui.element("div").classes("input-card" + (" missing" if workholding_missing else "")):
        ui.label("Workholding").classes("input-card-title")
        ui.label(_sources_text(["workholding_method"], sources)).classes("input-card-source")
        if workholding_missing:
            ui.label("Missing required workholding method.").classes("missing-note")
        workholding = ui.select(
            list(machine.machine.workholding),
            value=inputs.get("workholding_method") or [],
            label="Workholding",
            multiple=True,
        ).props("outlined dense use-chips").classes("w-full")
    widgets.append({"name": "workholding_method", "label": "Workholding", "required": True, "control": workholding, "state_key": "workholding_method"})
    return widgets


def _sources_text(names: list[str], sources: dict[str, str]) -> str:
    used = [sources[name] for name in names if name in sources]
    if not used:
        return "source: user input needed"
    return "source: " + ", ".join(dict.fromkeys(used))


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _input_label(name: str) -> str:
    return name.replace("_", " ").title()


def _widget_has_value(control: Any) -> bool:
    if isinstance(control, dict) and control.get("kind") == "group":
        return all(_widget_has_value(child) for child in control["controls"])
    value = getattr(control, "value", None)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return value is not None


def _iter_controls(control: Any) -> list[Any]:
    if isinstance(control, dict) and control.get("kind") == "group":
        return control["controls"]
    return [control]


def _capture_input_values(state: dict[str, Any], widgets: list[dict[str, Any]], notes_control: Any) -> None:
    inputs = state.setdefault("inputs", {})
    for widget in widgets:
        key = widget.get("state_key")
        if key:
            inputs[key] = getattr(widget["control"], "value", None)
    inputs["planning_notes"] = notes_control.value or ""


def _stock_size_from_geometry(geometry: dict[str, Any]) -> str | None:
    entities = {entity["id"]: entity for entity in geometry["entities"]}
    frame = next((node for node in geometry.get("entity_map", []) if node["role"] == "frame"), None)
    if frame is None:
        return None
    entity = entities.get(frame["entity"])
    if entity is None or not entity.get("bounding_box"):
        return None
    box = entity["bounding_box"]
    width = box["max"]["x"] - box["min"]["x"]
    height = box["max"]["y"] - box["min"]["y"]
    return f'{width:.3f} x {height:.3f} {geometry["units"]["length"]} frame'


def _planning_intro_lines(context: dict[str, Any]) -> list[str]:
    planner: PlannerFile = context["planner"]
    artifacts: ProjectArtifacts = context["artifacts"]
    known: dict[str, str] = context["known"]
    missing: list[dict[str, str]] = context["missing"]
    result = artifacts.clean_result

    lines = [
        f"Loaded {artifacts.original_dxf.name}.",
        (
            "Generated geom.yaml: "
            f"{result.closed_loops} closed loops, {result.open_paths} open paths, "
            f"{result.endpoints_snapped} endpoints snapped."
        ),
        "",
        "Known planning inputs:",
    ]
    for name, value in known.items():
        lines.append(f"- {name}: {value}")

    lines.append("")
    if missing:
        lines.append("Missing required inputs:")
        for item in missing:
            lines.append(f"- {item['name']}: {item['description']}")
    else:
        lines.append("Missing required inputs: none")

    lines.extend(
        [
            "",
            "Planner defaults:",
            f"- max_tools: {planner.defaults.max_tools}",
            f"- default_tool: {planner.defaults.default_tool}",
            f"- milling_direction: {planner.defaults.milling_direction}",
            f"- part_holding: {', '.join(planner.defaults.part_holding)}",
            "",
            "Planner guidance:",
        ]
    )
    for section_name, items in (
        ("workholding", planner.operation_advice.workholding),
        ("tools", planner.operation_advice.tools),
        ("geometry", planner.operation_advice.geometry),
    ):
        lines.append(f"{section_name}:")
        for item in items:
            lines.append(f"- {item}")
    lines.append("")
    if missing:
        lines.append("Please answer the missing inputs, then click Generate plan.")
    else:
        lines.append("Required inputs are present. Add any machining intent, then click Generate plan.")
    return lines


def _render_crumbs(state: dict, active: str) -> None:
    crumbs = state.get("crumbs")
    if crumbs is None:
        return
    crumbs.clear()
    with crumbs:
        choose_class = "nav-active" if active == "choose" else "nav-link"
        ui.button(
            "choose dxf",
            icon="upload_file",
            on_click=lambda: render_initial_state(state["content"], state, service.load_machine()),
        ).props("flat dense").classes(choose_class)
        ui.label(">").classes("step-separator")
        plan_class = "nav-active" if active == "plan" else ("nav-link" if state.get("project") else "nav-muted")
        plan_button = ui.button(
            "operation plan",
            icon="fact_check",
            on_click=lambda: _return_to_plan(state),
        ).props("flat dense").classes(plan_class)
        if not state.get("project"):
            plan_button.disable()
        ui.label(">").classes("step-separator")
        ui.button("toolpaths", icon="route").props("flat dense disable").classes("nav-muted")


def _return_to_plan(state: dict[str, Any]) -> None:
    artifacts = state.get("project")
    if artifacts is None:
        return
    render_project_state(state["content"], state, service.load_machine(), artifacts)


def _yaml_pre(path: Path) -> str:
    return f'<pre class="yaml-view">{escape(path.read_text(encoding="utf-8"))}</pre>'


async def handle_upload(event: Any, state: dict, machine: MachineFile, content_host) -> None:
    uploaded_file = event.file
    filename = uploaded_file.name or "uploaded.dxf"
    upload_bytes = await uploaded_file.read()
    try:
        artifacts = service.create_project_from_upload(filename, upload_bytes)
    except Exception as exc:  # pragma: no cover - surfaced in UI
        ui.notify(f"Failed to process DXF: {exc}", type="negative")
        return

    state["project"] = artifacts
    render_project_state(content_host, state, machine, artifacts)
    ui.notify("geom.yaml generated", type="positive")


def render_job_display(artifacts: ProjectArtifacts) -> str:
    svg = _job_scene_svg(artifacts.geometry_svg)
    return f"""
    <div class="dxf-viewer">
      <div class="viewer-toolbar">
        <button type="button" title="Zoom to extents" onclick="dxfwizInitViewer(); dxfwizResetView()">{_icon_fit_screen()}</button>
      </div>
      {svg}
    </div>
    <style>
      .dxf-viewer {{
        height: 100%;
        border: 1px solid #cbd5e1;
        background: #ffffff;
        border-radius: 8px;
        position: relative;
        overflow: hidden;
        box-shadow: 0 16px 40px rgba(15, 23, 42, 0.08);
      }}
      .viewer-toolbar {{
        position: absolute;
        top: 10px;
        left: 10px;
        right: auto;
        z-index: 2;
        display: flex;
        gap: 12px;
        align-items: center;
        pointer-events: none;
      }}
      .viewer-toolbar button {{
        pointer-events: auto;
        border: 1px solid #94a3b8;
        border-radius: 5px;
        background: #ffffff;
        color: #0f172a;
        width: 32px;
        height: 32px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0;
        font: 13px Segoe UI, Arial, sans-serif;
      }}
      .viewer-toolbar button:hover {{ background: #eff6ff; border-color: #2563eb; color: #1d4ed8; }}
      .viewer-toolbar svg {{ width: 18px; height: 18px; stroke: currentColor; }}
      .viewer-toolbar span {{
        color: #475569;
        background: rgba(255,255,255,0.88);
        padding: 4px 7px;
        border-radius: 4px;
        font: 13px Segoe UI, Arial, sans-serif;
      }}
      .scene-svg {{ width: 100%; height: 100%; display: block; cursor: grab; outline: none; }}
      .dxf-viewer, .scene-svg, .scene-svg text, .scene-svg tspan {{
        user-select: none;
        -webkit-user-select: none;
      }}
      .scene-svg:focus {{ box-shadow: inset 0 0 0 2px #2563eb; }}
      .scene-svg.panning {{ cursor: grabbing; }}
      #dxfwiz-scene .role-ignored {{ display: none; }}
    </style>
    """


def render_display(machine_file: MachineFile, artifacts: ProjectArtifacts | None) -> str:
    machine = machine_file.machine
    envelope = machine.work_envelope
    machine_width = envelope.x.max - envelope.x.min
    machine_height = envelope.y.max - envelope.y.min
    geometry_svg = _nested_geometry_svg(artifacts.geometry_svg) if artifacts else ""
    viewbox = _combined_viewbox(machine_width, machine_height, artifacts)
    details = _machine_details(machine_file, artifacts)
    axis_length = min(machine_width, machine_height) * 0.12
    axis_stroke = max(min(machine_width, machine_height) * 0.012, 0.18)

    return f"""
    <div class="dxf-viewer">
      <div class="viewer-toolbar">
        <button type="button" title="Zoom in" onclick="dxfwizZoom(0.82)">{_icon_zoom_in()}</button>
        <button type="button" title="Zoom out" onclick="dxfwizZoom(1.18)">{_icon_zoom_out()}</button>
        <button type="button" title="Zoom to extents" onclick="dxfwizResetView()">{_icon_fit_screen()}</button>
        <span>{details}</span>
      </div>
      <svg id="dxfwiz-scene" class="scene-svg" viewBox="{viewbox}" tabindex="0" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <marker id="machine-x-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
            <path d="M 0 0 L 8 4 L 0 8 z" fill="#dc2626" />
          </marker>
          <marker id="machine-y-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
            <path d="M 0 0 L 8 4 L 0 8 z" fill="#16a34a" />
          </marker>
        </defs>
        <rect class="machine-bed" x="0" y="0" width="{machine_width:.6f}" height="{machine_height:.6f}" />
        <text class="machine-label" x="1.2" y="1.5">{machine.name}</text>
        <text class="machine-label muted" x="1.2" y="2.55">{machine_width:.3f} x {machine_height:.3f} {machine_file.units.length}</text>
        <g class="machine-origin">
          <circle cx="0" cy="{machine_height:.6f}" r="{axis_stroke * 0.9:.6f}" />
          <line class="x-axis" x1="0" y1="{machine_height:.6f}" x2="{axis_length:.6f}" y2="{machine_height:.6f}" marker-end="url(#machine-x-arrow)" />
          <line class="y-axis" x1="0" y1="{machine_height:.6f}" x2="0" y2="{machine_height - axis_length:.6f}" marker-end="url(#machine-y-arrow)" />
          <text class="axis-label x-label" x="{axis_length + axis_stroke:.6f}" y="{machine_height - axis_stroke:.6f}">X</text>
          <text class="axis-label y-label" x="{axis_stroke:.6f}" y="{machine_height - axis_length - axis_stroke:.6f}">Y</text>
        </g>
        {geometry_svg}
      </svg>
    </div>
    <style>
      .dxf-viewer {{
        height: 100%;
        border: 1px solid #cbd5e1;
        background: #ffffff;
        border-radius: 8px;
        position: relative;
        overflow: hidden;
        box-shadow: 0 16px 40px rgba(15, 23, 42, 0.08);
      }}
      .viewer-toolbar {{
        position: absolute;
        top: 10px;
        left: 10px;
        right: 10px;
        z-index: 2;
        display: flex;
        gap: 12px;
        align-items: center;
        pointer-events: none;
      }}
      .viewer-toolbar button {{
        pointer-events: auto;
        border: 1px solid #94a3b8;
        border-radius: 5px;
        background: #ffffff;
        color: #0f172a;
        width: 32px;
        height: 32px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0;
        font: 13px Segoe UI, Arial, sans-serif;
      }}
      .viewer-toolbar button:hover {{ background: #eff6ff; border-color: #2563eb; color: #1d4ed8; }}
      .viewer-toolbar svg {{ width: 18px; height: 18px; stroke: currentColor; }}
      .viewer-toolbar span {{
        color: #475569;
        background: rgba(255,255,255,0.88);
        padding: 4px 7px;
        border-radius: 4px;
        font: 13px Segoe UI, Arial, sans-serif;
      }}
      .scene-svg {{ width: 100%; height: 100%; display: block; cursor: grab; outline: none; }}
      .dxf-viewer, .scene-svg, .scene-svg text, .scene-svg tspan {{
        user-select: none;
        -webkit-user-select: none;
      }}
      .scene-svg:focus {{ box-shadow: inset 0 0 0 2px #2563eb; }}
      .scene-svg.panning {{ cursor: grabbing; }}
      .machine-bed {{
        fill: #f8fafc;
        stroke: #94a3b8;
        stroke-width: 1.2px;
        vector-effect: non-scaling-stroke;
      }}
      .machine-label {{
        font: 0.42px Segoe UI, Arial, sans-serif;
        fill: #334155;
      }}
      .machine-label.muted {{ fill: #64748b; }}
      .machine-origin circle {{ fill: #0f172a; }}
      .machine-origin line {{
        stroke-width: {axis_stroke:.6f};
        vector-effect: non-scaling-stroke;
      }}
      .machine-origin .x-axis {{ stroke: #dc2626; }}
      .machine-origin .y-axis {{ stroke: #16a34a; }}
      .axis-label {{
        font: 1.2px Segoe UI, Arial, sans-serif;
        font-weight: 700;
      }}
      .x-label {{ fill: #dc2626; }}
      .y-label {{ fill: #16a34a; }}
      #dxfwiz-scene .role-frame .entity-outline {{ stroke: #16a34a; }}
      #dxfwiz-scene .role-part .entity-outline {{ stroke: #1e3a8a; stroke-width: 2.7px; }}
      #dxfwiz-scene .role-cutout .entity-outline {{ stroke: #60a5fa; }}
      #dxfwiz-scene .role-ignored {{ display: none; }}
    </style>
    """


def _icon_zoom_in() -> str:
    return """
    <svg viewBox="0 0 24 24" fill="none" stroke-width="2" aria-hidden="true">
      <circle cx="11" cy="11" r="7"></circle>
      <path d="M11 8v6M8 11h6M16.5 16.5 21 21"></path>
    </svg>
    """


def _icon_zoom_out() -> str:
    return """
    <svg viewBox="0 0 24 24" fill="none" stroke-width="2" aria-hidden="true">
      <circle cx="11" cy="11" r="7"></circle>
      <path d="M8 11h6M16.5 16.5 21 21"></path>
    </svg>
    """


def _icon_fit_screen() -> str:
    return """
    <svg viewBox="0 0 24 24" fill="none" stroke-width="2" aria-hidden="true">
      <path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"></path>
      <path d="M9 4 4 9M15 4l5 5M20 15l-5 5M9 20l-5-5"></path>
    </svg>
    """


def _viewer_script() -> str:
    return """
    <script>
      window.dxfwizResetView = function() {
        const svg = document.getElementById('dxfwiz-scene');
        if (!svg || !svg.dataset.initialViewBox) return;
        svg.setAttribute('viewBox', svg.dataset.initialViewBox);
        svg.dataset.currentViewBox = svg.dataset.initialViewBox;
      };
      window.dxfwizZoom = function(factor) {
        const svg = document.getElementById('dxfwiz-scene');
        if (!svg || !svg.dataset.currentViewBox) return;
        const vb = svg.dataset.currentViewBox.split(' ').map(Number);
        const cx = vb[0] + vb[2] / 2;
        const cy = vb[1] + vb[3] / 2;
        vb[0] = cx - vb[2] * factor / 2;
        vb[1] = cy - vb[3] * factor / 2;
        vb[2] *= factor;
        vb[3] *= factor;
        svg.dataset.currentViewBox = vb.join(' ');
        svg.setAttribute('viewBox', svg.dataset.currentViewBox);
        svg.focus();
      };

      window.dxfwizInitViewer = function() {
        const svg = document.getElementById('dxfwiz-scene');
        if (!svg || svg.dataset.dxfwizReady === '1') return;
        svg.dataset.dxfwizReady = '1';
        const initial = svg.getAttribute('viewBox');
        svg.dataset.initialViewBox = initial;
        svg.dataset.currentViewBox = initial;
        let dragging = false;
        let last = null;
        function current() { return svg.dataset.currentViewBox.split(' ').map(Number); }
        function setViewBox(vb) {
          svg.dataset.currentViewBox = vb.join(' ');
          svg.setAttribute('viewBox', svg.dataset.currentViewBox);
        }
        svg.addEventListener('selectstart', function(event) { event.preventDefault(); });
        svg.addEventListener('focus', function() { svg.dataset.zoomActive = '1'; });
        svg.addEventListener('wheel', function(event) {
          if (svg.dataset.zoomActive !== '1') return;
          event.preventDefault();
          const vb = current();
          const rect = svg.getBoundingClientRect();
          const mx = vb[0] + (event.clientX - rect.left) / rect.width * vb[2];
          const my = vb[1] + (event.clientY - rect.top) / rect.height * vb[3];
          const factor = event.deltaY < 0 ? 0.88 : 1.14;
          vb[0] = mx - (mx - vb[0]) * factor;
          vb[1] = my - (my - vb[1]) * factor;
          vb[2] *= factor;
          vb[3] *= factor;
          setViewBox(vb);
        }, { passive: false });
        svg.addEventListener('pointerdown', function(event) {
          event.preventDefault();
          window.getSelection && window.getSelection().removeAllRanges();
          svg.focus();
          svg.dataset.zoomActive = '1';
          dragging = true;
          last = [event.clientX, event.clientY];
          svg.classList.add('panning');
          svg.setPointerCapture(event.pointerId);
        });
        svg.addEventListener('pointermove', function(event) {
          if (!dragging || !last) return;
          const vb = current();
          const rect = svg.getBoundingClientRect();
          const dx = (event.clientX - last[0]) / rect.width * vb[2];
          const dy = (event.clientY - last[1]) / rect.height * vb[3];
          vb[0] -= dx;
          vb[1] -= dy;
          last = [event.clientX, event.clientY];
          setViewBox(vb);
        });
        svg.addEventListener('pointerup', function(event) {
          dragging = false;
          last = null;
          svg.classList.remove('panning');
          try { svg.releasePointerCapture(event.pointerId); } catch (_) {}
        });
      };
      window.dxfwizInitViewer();
      new MutationObserver(() => window.dxfwizInitViewer())
        .observe(document.body, { childList: true, subtree: true });
    </script>
    """


def _machine_details(machine_file: MachineFile, artifacts: ProjectArtifacts | None) -> str:
    machine = machine_file.machine
    z = machine.work_envelope.z
    details = [
        f"{machine_file.units.length}",
        f"Z {z.min:g}..{z.max:g}",
        f"{len(machine_file.tools)} tools configured",
    ]
    if artifacts:
        units = artifacts.geometry["units"]
        details.append(f"geom: {units['length']} ({units['source']})")
        details.append(f"{artifacts.geometry['summary']['entity_count']} entities")
    return " | ".join(details)


def _job_details(artifacts: ProjectArtifacts) -> str:
    units = artifacts.geometry["units"]
    summary = artifacts.geometry["summary"]
    details = [
        f"geom: {units['length']} ({units['source']})",
        f"{summary['entity_count']} entities",
        f"{summary['ignored_count']} ignored",
    ]
    return " | ".join(details)


def _job_scene_svg(svg_path: Path) -> str:
    svg = svg_path.read_text(encoding="utf-8")
    viewbox = _svg_viewbox(svg_path)
    viewbox_attr = ""
    if viewbox:
        viewbox_text = " ".join(f"{value:.6f}" for value in viewbox)
        viewbox_attr = f' data-initial-view-box="{viewbox_text}" data-current-view-box="{viewbox_text}"'
    return re.sub(
        r"<svg\b([^>]*)>",
        rf'<svg id="dxfwiz-scene" class="scene-svg"\1 tabindex="0"{viewbox_attr}>',
        svg,
        count=1,
    )


def _combined_viewbox(
    machine_width: float,
    machine_height: float,
    artifacts: ProjectArtifacts | None,
) -> str:
    x1, y1, x2, y2 = 0.0, 0.0, machine_width, machine_height
    if artifacts:
        match = _svg_viewbox(artifacts.geometry_svg)
        if match:
            gx, gy, gw, gh = match
            x1 = min(x1, gx)
            y1 = min(y1, gy)
            x2 = max(x2, gx + gw)
            y2 = max(y2, gy + gh)
    pad = max(x2 - x1, y2 - y1) * 0.06
    return f"{x1 - pad:.6f} {y1 - pad:.6f} {(x2 - x1) + pad * 2:.6f} {(y2 - y1) + pad * 2:.6f}"


def _nested_geometry_svg(svg_path: Path) -> str:
    svg = svg_path.read_text(encoding="utf-8")
    match = _svg_viewbox(svg_path)
    if not match:
        return svg
    x, y, width, height = match
    svg = re.sub(
        r"<svg\b[^>]*>",
        (
            f'<svg class="geometry-layer" x="{x:.6f}" y="{y:.6f}" '
            f'width="{width:.6f}" height="{height:.6f}" '
            f'viewBox="{x:.6f} {y:.6f} {width:.6f} {height:.6f}" '
            f'xmlns="http://www.w3.org/2000/svg">'
        ),
        svg,
        count=1,
    )
    return svg


def _svg_viewbox(svg_path: Path) -> tuple[float, float, float, float] | None:
    svg = svg_path.read_text(encoding="utf-8")
    match = re.search(r'viewBox="([^"]+)"', svg)
    if not match:
        return None
    values = [float(value) for value in match.group(1).split()]
    if len(values) != 4:
        return None
    return values[0], values[1], values[2], values[3]


if __name__ in {"__main__", "__mp_main__"}:
    main()
