from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from html import escape
from pathlib import Path
from typing import Any
from uuid import uuid4

from nicegui import app, ui

from dxfwiz.api import router as api_router
from dxfwiz.config import load_config
from dxfwiz.logging_config import configure_logging
from dxfwiz.planning import (
    PlanningRequest,
    PlanningResponse,
    generate_operation_plan,
    load_system_planner_advice,
)
from dxfwiz.planning.ai_stats import ai_stats_summary
from dxfwiz.planning.yaml_format import dump_operation_yaml
from dxfwiz.schemas import GeometryFile, JobFile, MachineFile, OperationInputsFile, PlannerFile
from dxfwiz.toolpaths import ToolpathRequest, ToolpathResponse
from dxfwiz.ui.service import ProjectArtifacts, ProjectService
from dxfwiz.yaml_io import dump_yaml_file, load_yaml_file


ROOT_DIR = Path(__file__).resolve().parents[3]
WORKSPACE_DIR = ROOT_DIR / "workspace"
EXAMPLES_DIR = ROOT_DIR / "examples"

service = ProjectService(
    workspace_dir=WORKSPACE_DIR,
    machine_yaml=EXAMPLES_DIR / "machine.yaml",
    planner_yaml=EXAMPLES_DIR / "planner.yaml",
)
app.add_static_files("/workspace", WORKSPACE_DIR)
app.include_router(api_router)
logger = logging.getLogger(__name__)


def main() -> None:
    configure_logging()
    logger.info("Starting DXF Wizard UI")
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
            height: 100%; min-height: 0; display: grid; grid-template-columns: minmax(0, 1fr) 460px;
          }
          .display-card { min-height: 0; padding: 8px; background: #eef3f8; }
          .planning-panel {
            border-left: 1px solid #dbe3ef; background: #ffffff; min-height: 0; height: 100%;
            display: flex; flex-direction: column;
          }
          .viewer-host { height: 100%; min-height: 0; }
          .planning-scroll { flex: 1; min-height: 0; overflow: auto; padding: 14px; }
          .planning-footer { border-top: 1px solid #e2e8f0; padding: 12px; background: #ffffff; }
          .planner-busy {
            border: 1px solid #bfdbfe; border-radius: 7px; background: #eff6ff;
            padding: 9px; margin-top: 10px;
          }
          .planner-busy.failed { border-color: #fecaca; background: #fff7f7; }
          .planner-busy.done { border-color: #bbf7d0; background: #f0fdf4; }
          .graphics-area { height: 100%; min-height: 0; display: flex; flex-direction: column; gap: 8px; }
          .graphics-toolbar {
            flex: 0 0 auto; display: flex; align-items: center; gap: 8px;
            background: #ffffff; border: 1px solid #dbe3ef; border-radius: 7px; padding: 7px;
          }
          .graphics-toolbar .q-btn { min-height: 32px; }
          .graphics-toolbar .toggle-on { background: #dbeafe; color: #1d4ed8; }
          .graphics-host { flex: 1; min-height: 0; }
          .summary-grid { display: grid; grid-template-columns: 1fr auto; gap: 8px 14px; font-size: 13px; }
          .summary-key { color: #64748b; }
          .summary-value { color: #0f172a; font-weight: 650; text-align: right; }
          .input-card {
            border: 1px solid #dbe3ef; border-radius: 7px; background: #f8fafc;
            padding: 10px; margin-bottom: 10px;
          }
          .input-card-title { font-weight: 750; color: #0f172a; font-size: 13px; }
          .input-card-title-row { display: flex; align-items: center; gap: 8px; margin-bottom: 2px; }
          .input-card-source { color: #64748b; font-size: 12px; margin-top: 2px; margin-bottom: 8px; }
          .input-card.missing { border-color: #fca5a5; background: #fff7f7; }
          .input-card.missing .input-card-title { color: #b91c1c; }
          .input-card.ok { border-color: #86efac; background: #f0fdf4; }
          .input-status-ok { color: #16a34a; }
          .input-status-missing { color: #dc2626; }
          .missing-note { color: #b91c1c; font-size: 12px; font-weight: 650; margin-bottom: 8px; }
          .input-card .q-field { font-size: 13px; }
          .field-row { display: grid; grid-template-columns: minmax(0, 1fr) 88px; gap: 8px; }
          .yaml-view {
            background: #0f172a; color: #e2e8f0; border-radius: 7px; padding: 14px;
            min-height: 0; overflow: visible; white-space: pre; font: 12px Consolas, monospace;
            width: max-content; min-width: 100%;
          }
          .settings-screen { height: 100%; padding: 18px; overflow: auto; background: #eef3f8; }
          .settings-card {
            min-height: 100%; background: #ffffff; border: 1px solid #dbe3ef; border-radius: 8px;
            padding: 16px; display: flex; flex-direction: column; box-shadow: 0 16px 40px rgba(15, 23, 42, 0.08);
          }
          .ai-stat-grid { display: grid; grid-template-columns: 1fr auto; gap: 10px 18px; font-size: 14px; }
          .ai-stat-key { color: #64748b; }
          .ai-stat-value { color: #0f172a; font-weight: 750; text-align: right; }
          .ai-meter { height: 10px; border-radius: 999px; background: #e2e8f0; overflow: hidden; }
          .ai-meter-fill { height: 100%; background: #2563eb; }
          .toolpaths-grid {
            height: 100%; min-height: 0; display: grid; grid-template-columns: minmax(0, 1fr) 7px 430px;
          }
          .toolpaths-grid .display-card { min-width: 0; }
          .splitter {
            width: 7px; cursor: col-resize; background: #dbe3ef; border-left: 1px solid #cbd5e1;
            border-right: 1px solid #cbd5e1;
          }
          .splitter:hover, .splitter.dragging { background: #93c5fd; }
          .toolpath-panel {
            min-height: 0; height: 100%; min-width: 320px; overflow: auto;
            background: #ffffff; padding: 14px;
          }
          .issue-list { border: 1px solid #dbe3ef; border-radius: 7px; padding: 10px; margin-top: 10px; background: #f8fafc; }
          .issue-error { color: #b91c1c; }
          .issue-warning { color: #92400e; }
          .op-output {
            overflow: auto; white-space: pre; font: 12px Consolas, monospace;
            background: #0f172a; color: #e2e8f0; border-radius: 7px; padding: 10px; margin-top: 10px;
            max-height: calc(100vh - 245px);
          }
          .operation-groups { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
          .operation-group {
            border: 1px solid #dbe3ef; border-radius: 7px; background: #f8fafc; padding: 8px;
          }
          .operation-group-title {
            width: 100%; border: 0; background: transparent; color: #0f172a; cursor: pointer;
            display: flex; align-items: center; justify-content: space-between; font-weight: 750;
            font-size: 13px; padding: 4px;
          }
          .operation-card {
            width: 100%; margin-top: 6px; border: 1px solid #dbe3ef; border-radius: 7px;
            background: #ffffff; padding: 8px; cursor: pointer; text-align: left;
          }
          .operation-card:hover, .operation-group-title:hover { background: #eff6ff; }
          .operation-card-title { font-weight: 750; color: #0f172a; font-size: 13px; }
          .operation-card-meta { color: #475569; font-size: 12px; margin-top: 3px; }
          .operation-yaml-popover {
            position: fixed; top: 76px; right: 22px; z-index: 50; width: min(560px, calc(100vw - 44px));
            max-height: calc(100vh - 112px); border: 1px solid #94a3b8; border-radius: 8px;
            background: #ffffff; box-shadow: 0 22px 70px rgba(15, 23, 42, 0.22); overflow: hidden;
          }
          .operation-yaml-popover header {
            height: 40px; display: flex; align-items: center; justify-content: space-between;
            padding: 0 10px 0 14px; border-bottom: 1px solid #dbe3ef; font-weight: 750;
          }
          .operation-yaml-popover button {
            border: 0; background: transparent; cursor: pointer; color: #475569; font-size: 20px;
          }
          .operation-yaml-popover pre {
            margin: 0; padding: 12px; overflow: auto; max-height: calc(100vh - 154px);
            background: #0f172a; color: #e2e8f0; font: 12px Consolas, monospace;
          }
          #dxfwiz-scene .selected-entity .entity-outline,
          #dxfwiz-scene .selected-entity.workholding-outline,
          #dxfwiz-scene .selected-entity .workholding-outline {
            stroke: #dc2626 !important;
            stroke-width: 4px !important;
          }
          #dxfwiz-scene .selected-entity.entity-name-label .entity-name-text,
          #dxfwiz-scene .selected-entity.workholding-label,
          #dxfwiz-scene .selected-entity .workholding-label {
            fill: #dc2626 !important;
          }
          #dxfwiz-scene.hide-entity-labels .entity-name-label,
          #dxfwiz-scene.hide-entity-labels .workholding-label {
            display: none;
          }
          #dxfwiz-scene.hide-dimension-labels .diameter-label,
          #dxfwiz-scene.hide-dimension-labels .leader-line,
          #dxfwiz-scene.hide-dimension-labels .callout-bubble {
            display: none;
          }
        </style>
        """
    )
    with ui.element("div").classes("app-shell"):
        with ui.element("div").classes("topbar"):
            ui.label("DXF Wizard").classes("brand")
            with ui.row().classes("items-center gap-2") as crumbs:
                state["crumbs"] = crumbs
            ui.space()
            ui.button(
                "AI",
                icon="psychology",
                on_click=render_ai_status_dialog,
            ).props("flat dense").classes("nav-link")
            ui.button(
                "Settings",
                icon="settings",
                on_click=lambda: render_settings_state(state["content"], state),
            ).props("flat dense").classes("nav-link")

        with ui.element("div").classes("content-host") as content:
            state["content"] = content

    render_initial_state(content, state, machine)
    ui.timer(0.1, lambda: ui.run_javascript(_viewer_javascript()), once=True)

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
                _render_graphics_area(artifacts, state)
            with ui.element("div").classes("planning-panel"):
                ui.label("Planning").classes("px-4 pt-4 text-lg font-semibold text-slate-900")
                ui.button(
                    "Download geom.yaml",
                    icon="download",
                    on_click=lambda: ui.download(artifacts.geom_yaml),
                ).props("flat color=primary dense").classes("mx-4 mt-1")
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
                    generate_indicator = ui.icon("cancel").classes("input-status-missing text-xl mt-2")
                    with ui.column().classes("planner-busy w-full") as busy_box:
                        ui.linear_progress().props("indeterminate color=primary").classes("w-full")
                        busy_label = ui.label("Waiting to generate.").classes("text-sm text-slate-700")
                        busy_detail = ui.label("").classes("text-xs text-slate-500")
                    busy_box.style("display: none")

                    generate_state = {
                        "ready": False,
                        "busy": False,
                        "request_id": None,
                        "started_at": None,
                    }

                    def update_busy_status() -> None:
                        if not generate_state["busy"] or generate_state["started_at"] is None:
                            return
                        elapsed = time.monotonic() - generate_state["started_at"]
                        busy_label.text = f"Planner request is running... {elapsed:.0f}s elapsed"
                        busy_detail.text = f"Request {generate_state['request_id']} is still active."

                    ui.timer(1.0, update_busy_status)

                    def update_generate_state() -> None:
                        if generate_state["busy"]:
                            generate_button.disable()
                            return
                        _capture_input_values(state, input_widgets, intent_input)
                        _update_input_section_status(input_widgets)
                        missing = [
                            widget["label"]
                            for widget in input_widgets
                            if widget["required"] and not _widget_has_value(widget["control"])
                        ]
                        if missing:
                            generate_state["ready"] = False
                            generate_button.disable()
                            generate_indicator.name = "cancel"
                            generate_indicator.classes(replace="input-status-missing text-xl mt-2")
                        else:
                            generate_state["ready"] = True
                            generate_button.enable()
                            generate_indicator.name = "check_circle"
                            generate_indicator.classes(replace="input-status-ok text-xl mt-2")

                    async def generate_plan() -> None:
                        if not generate_state["ready"] or generate_state["busy"]:
                            return
                        generate_state["busy"] = True
                        generate_state["request_id"] = uuid4().hex[:8]
                        generate_state["started_at"] = time.monotonic()
                        generate_button.disable()
                        busy_box.classes(replace="planner-busy w-full")
                        busy_label.text = "Planner request is starting..."
                        busy_detail.text = f"Request {generate_state['request_id']} is active."
                        busy_box.style("")
                        state["inputs"]["planning_notes"] = intent_input.value or ""
                        request = _planning_request(machine, planner, artifacts, state)
                        navigated = False
                        try:
                            response = await _post_plan_request(request)
                            elapsed = time.monotonic() - generate_state["started_at"]
                            state["inputs"]["op_yaml"] = response.op_yaml
                            _write_project_op_yaml(artifacts, response.op_yaml)
                            if response.geometry:
                                state["geometry"] = response.geometry
                                _write_project_geometry(artifacts, response.geometry)
                            if response.errors:
                                state["inputs"]["op_yaml"] = _issues_yaml(response)
                                ui.notify("Plan has errors to resolve.", type="negative")
                            elif response.warnings:
                                ui.notify("Plan generated with warnings.", type="warning")
                            else:
                                ui.notify("Plan generated.", type="positive")
                            state["plan_response"] = response
                            busy_box.classes(replace="planner-busy done w-full")
                            busy_label.text = f"Planner request completed in {elapsed:.1f}s."
                            navigated = True
                            render_toolpaths_state(state["content"], state)
                        except Exception as exc:
                            logger.exception("Operation plan generation failed")
                            elapsed = time.monotonic() - generate_state["started_at"]
                            busy_box.classes(replace="planner-busy failed w-full")
                            busy_label.text = f"Planner request failed after {elapsed:.1f}s."
                            busy_detail.text = str(exc)
                            ui.notify(f"Plan generation failed: {exc}", type="negative")
                        finally:
                            generate_state["busy"] = False
                            generate_state["started_at"] = None
                            if not navigated:
                                update_generate_state()

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
                with ui.tab_panels(tabs, value=machine_tab).classes("w-full"):
                    with ui.tab_panel(machine_tab):
                        ui.html(_yaml_pre(EXAMPLES_DIR / "machine.yaml"))
                    with ui.tab_panel(planner_tab):
                        ui.html(_yaml_pre(EXAMPLES_DIR / "planner.yaml"))


def render_ai_status_dialog() -> None:
    config = load_config()
    stats = ai_stats_summary(config.gemini.token_limit)
    last_call = stats["last_call"] or {}
    with ui.dialog() as dialog, ui.card().classes("w-[560px] max-w-[calc(100vw-32px)]"):
        with ui.row().classes("w-full items-center"):
            ui.icon("psychology").classes("text-blue-700 text-2xl")
            ui.label("AI Planner Status").classes("text-lg font-semibold text-slate-900")
            ui.space()
            ui.button(icon="close", on_click=dialog.close).props("flat round dense")
        ui.label("Local usage estimates for planner calls made from this app.").classes(
            "text-sm text-slate-600"
        )
        percent = min(stats["token_percent"], 100)
        with ui.element("div").classes("ai-meter w-full mt-2"):
            ui.element("div").classes("ai-meter-fill").style(f"width: {percent:.1f}%")
        rows = [
            ("Planner mode", config.planner.mode),
            ("Model", config.gemini.model),
            ("Token usage", f"{stats['total_tokens']:,} / {stats['token_limit']:,}"),
            ("Remaining", f"{stats['remaining_tokens']:,}"),
            ("Budget used", f"{stats['token_percent']:.1f}%"),
            ("Calls", f"{stats['call_count']} total | {stats['success_count']} ok | {stats['failure_count']} failed"),
            ("Average response", f"{stats['average_response_seconds']:.2f} s"),
        ]
        if last_call:
            rows.extend(
                [
                    ("Last call", str(last_call.get("timestamp", ""))),
                    ("Last tokens", f"{int(last_call.get('total_tokens') or 0):,}"),
                    ("Last duration", f"{float(last_call.get('elapsed_seconds') or 0):.2f} s"),
                    ("Last result", "ok" if last_call.get("success") else "failed"),
                ]
            )
        with ui.element("div").classes("ai-stat-grid mt-4 w-full"):
            for key, value in rows:
                ui.label(key).classes("ai-stat-key")
                ui.label(str(value)).classes("ai-stat-value")
        if last_call.get("error"):
            ui.label(f"Last error: {last_call['error']}").classes("text-sm text-red-700 mt-3")
    dialog.open()


def render_toolpaths_state(content, state: dict) -> None:
    artifacts = state.get("project")
    if artifacts is None:
        return
    _render_crumbs(state, active="toolpaths")
    content.clear()
    with content:
        with ui.element("div").classes("toolpaths-grid"):
            with ui.element("div").classes("display-card"):
                _render_graphics_area(artifacts, state)
            ui.element("div").classes("splitter")
            with ui.element("div").classes("toolpath-panel"):
                ui.label("Toolpaths").classes("text-lg font-semibold text-slate-900")
                response: PlanningResponse | None = state.get("plan_response")
                _render_plan_issues(response)
                ui.button(
                    "Make adjustments",
                    icon="arrow_back",
                    on_click=lambda: _return_to_plan(state),
                ).props("flat color=primary").classes("w-full mt-3")
                ui.select(["uccnc"], value="uccnc", label="Post processor").props("outlined dense").classes("w-full mt-3")
                toolpath_button = ui.button("Generate toolpaths", icon="route").props("color=primary unelevated").classes("w-full mt-3")
                if response and response.errors:
                    toolpath_button.disable()
                async def generate_toolpaths_click() -> None:
                    if not response or not response.plan:
                        return
                    request = ToolpathRequest(
                        job=JobFile.model_validate(response.plan),
                        geometry=GeometryFile.model_validate(_geometry_from_state(artifacts, state)),
                        machine=service.load_machine(),
                        post="uccnc",
                        fixed_dxf=artifacts.fixed_dxf.read_text(encoding="utf-8", errors="ignore"),
                    )
                    toolpath_response = await _post_toolpath_request(request)
                    state["toolpath_response"] = toolpath_response
                    if toolpath_response.errors:
                        ui.notify("Toolpath generation has errors.", type="negative")
                    elif toolpath_response.warnings:
                        ui.notify("Toolpaths generated with warnings.", type="warning")
                    else:
                        ui.notify("Toolpaths generated.", type="positive")
                    if toolpath_response.gcode:
                        _write_project_gcode(artifacts, toolpath_response.gcode)
                    render_toolpaths_state(state["content"], state)
                toolpath_button.on("click", generate_toolpaths_click)
                ui.button(
                    "Download op.yaml",
                    icon="download",
                    on_click=lambda: ui.download(_op_yaml_path(artifacts)),
                ).props("flat color=primary").classes("w-full mt-2")
                gcode_path = _gcode_path(artifacts)
                gcode_button = ui.button(
                    "Download gcode",
                    icon="download",
                    on_click=lambda: ui.download(gcode_path),
                ).props("flat color=primary").classes("w-full mt-2")
                if not gcode_path.exists():
                    gcode_button.disable()
                _render_operation_groups(response)
                ui.label("op.yaml").classes("text-sm font-semibold text-slate-700 mt-4")
                ui.html(f'<pre class="op-output">{escape(state["inputs"].get("op_yaml", ""))}</pre>')
                toolpath_response: ToolpathResponse | None = state.get("toolpath_response")
                if toolpath_response:
                    _render_toolpath_issues(toolpath_response)
                    ui.label("gcode").classes("text-sm font-semibold text-slate-700 mt-4")
                    ui.html(f'<pre class="op-output">{escape(toolpath_response.gcode)}</pre>')


def _render_plan_issues(response: PlanningResponse | None) -> None:
    errors = response.errors if response else []
    warnings = response.warnings if response else []
    with ui.element("div").classes("issue-list"):
        ui.label("Plan Review").classes("text-sm font-semibold text-slate-800")
        if not errors and not warnings:
            ui.label("No warnings or errors.").classes("text-sm text-slate-600")
        for error in errors:
            ui.label(f"Error: {error.message}").classes("text-sm issue-error")
        for warning in warnings:
            ui.label(f"Warning: {warning.message}").classes("text-sm issue-warning")


def _render_toolpath_issues(response: ToolpathResponse) -> None:
    if not response.errors and not response.warnings:
        return
    with ui.element("div").classes("issue-list"):
        ui.label("Toolpath Review").classes("text-sm font-semibold text-slate-800")
        for error in response.errors:
            ui.label(f"Error: {error.message}").classes("text-sm issue-error")
        for warning in response.warnings:
            ui.label(f"Warning: {warning.message}").classes("text-sm issue-warning")


def _render_operation_groups(response: PlanningResponse | None) -> None:
    if response is None or not response.plan:
        return
    operations = response.plan.get("operations", [])
    operations_by_id = {operation["id"]: operation for operation in operations}
    groups = response.plan.get("operation_groups", [])
    if not groups:
        groups = [{"name": "operations", "operations": [operation["id"] for operation in operations]}]
    with ui.element("div").classes("operation-groups"):
        ui.label("Operation Plan").classes("text-sm font-semibold text-slate-700")
        for group in groups:
            group_operations = [
                operations_by_id[operation_id]
                for operation_id in group.get("operations", [])
                if operation_id in operations_by_id
            ]
            entity_ids = _operation_entity_ids(group_operations)
            with ui.element("div").classes("operation-group"):
                with ui.element("button").classes("operation-group-title").props("type=button") as button:
                    button.on("click", lambda _event, ids=entity_ids: _highlight_entities(ids))
                    ui.html(f"<span>{escape(group['name'].replace('_', ' ').title())}</span><span>{len(group_operations)}</span>")
                for operation in group_operations:
                    _render_operation_card(operation, _operation_entity_ids([operation]))


def _render_operation_card(operation: dict[str, Any], entity_ids: list[str]) -> None:
    description = operation.get("description") or operation["type"].replace("_", " ").title()
    if operation.get("tabs", {}).get("enabled"):
        description = f"{description}: tabs {operation['tabs'].get('count')}"
    title = f"{operation['id']} - {description}"
    meta = " | ".join(
        part
        for part in [
            operation.get("type"),
            f"entity {operation.get('entity')}",
            f"tool {operation.get('tool')}",
            f"depth {operation.get('depth')}",
        ]
        if part and not part.endswith("None")
    )
    with ui.element("button").classes("operation-card").props("type=button") as button:
        button.on("click", lambda _event, op=operation, ids=entity_ids: _show_operation_yaml(op, ids))
        ui.html(
            f'<div class="operation-card-title">{escape(title)}</div>'
            f'<div class="operation-card-meta">{escape(meta)}</div>'
        )


def _highlight_entities(entity_ids: list[str]) -> None:
    ui.run_javascript(
        _viewer_action_javascript(
            f"window.dxfwizHighlightEntities({json.dumps(entity_ids)});"
        )
    )


def _show_operation_yaml(operation: dict[str, Any], entity_ids: list[str]) -> None:
    yaml_text = dump_operation_yaml(operation)
    ui.run_javascript(
        _viewer_action_javascript(
            f"window.dxfwizShowOperationYaml({json.dumps(yaml_text)}, {json.dumps(entity_ids)});"
        )
    )


def _operation_entity_ids(operations: list[dict[str, Any]]) -> list[str]:
    result = []
    for operation in operations:
        entity_id = operation.get("entity")
        if entity_id and entity_id not in result:
            result.append(entity_id)
    return result


def _render_graphics_area(artifacts: ProjectArtifacts, state: dict[str, Any] | None = None) -> None:
    with ui.element("div").classes("graphics-area"):
        with ui.element("div").classes("graphics-toolbar"):
            ui.button(icon="zoom_in", on_click=lambda: ui.run_javascript(_viewer_action_javascript("window.dxfwizZoom(0.82);"))).props("flat dense").tooltip("Zoom in")
            ui.button(icon="zoom_out", on_click=lambda: ui.run_javascript(_viewer_action_javascript("window.dxfwizZoom(1.18);"))).props("flat dense").tooltip("Zoom out")
            ui.button(icon="fit_screen", on_click=lambda: ui.run_javascript(_viewer_action_javascript("window.dxfwizResetView();"))).props("flat dense").tooltip("Zoom to extents")
            ui.separator().props("vertical")
            ui.button(icon="rotate_left", on_click=lambda: ui.run_javascript(_viewer_action_javascript("window.dxfwizRotate(-90);"))).props("flat dense").tooltip("Rotate left")
            ui.button(icon="rotate_right", on_click=lambda: ui.run_javascript(_viewer_action_javascript("window.dxfwizRotate(90);"))).props("flat dense").tooltip("Rotate right")
            ui.separator().props("vertical")
            label_button = ui.button("Entities", icon="label").props("flat dense").classes("toggle-on")
            dim_button = ui.button("Dimensions", icon="straighten").props("flat dense").classes("toggle-on")
            label_button.on("click", lambda: ui.run_javascript(_viewer_action_javascript("window.dxfwizToggleLayer('entity-labels');")))
            dim_button.on("click", lambda: ui.run_javascript(_viewer_action_javascript("window.dxfwizToggleLayer('dimension-labels');")))
        ui.html(render_job_display(artifacts, state)).classes("graphics-host w-full")
        ui.timer(0.1, lambda: ui.run_javascript(_viewer_javascript()), once=True)


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

    stock_missing = not inputs.get("stock_xy") or not inputs.get("stock_material") or not inputs.get("stock_thickness")
    stock_card = ui.element("div").classes("input-card" + (" missing" if stock_missing else " ok"))
    with stock_card:
        stock_icon = _input_status_header("Stock", not stock_missing)
        ui.label(_sources_text(["stock_size", "stock_material", "stock_thickness"], sources)).classes("input-card-source")
        stock_note = ui.label("Missing required stock information.").classes("missing-note")
        if not stock_missing:
            stock_note.style("display: none")
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
            {"name": "stock_size", "label": "Stock", "required": True, "control": stock_xy, "state_key": "stock_xy", "section": "stock", "card": stock_card, "icon": stock_icon, "note": stock_note},
            {"name": "stock_material", "label": "Stock material", "required": True, "control": stock_material, "state_key": "stock_material", "section": "stock", "card": stock_card, "icon": stock_icon, "note": stock_note},
            {"name": "stock_units", "label": "Stock units", "required": False, "control": stock_units, "state_key": "stock_units", "section": "stock", "card": stock_card, "icon": stock_icon, "note": stock_note},
            {"name": "stock_thickness", "label": "Stock thickness", "required": True, "control": stock_thickness, "state_key": "stock_thickness", "section": "stock", "card": stock_card, "icon": stock_icon, "note": stock_note},
        ]
    )

    coordinate_missing = not inputs.get("z_zero_position") or not inputs.get("coordinate_system")
    coordinate_card = ui.element("div").classes("input-card" + (" missing" if coordinate_missing else " ok"))
    with coordinate_card:
        coordinate_icon = _input_status_header("Coordinate System", not coordinate_missing)
        ui.label(_sources_text(["z_zero_position", "coordinate_system"], sources)).classes("input-card-source")
        coordinate_note = ui.label("Missing required coordinate system information.").classes("missing-note")
        if not coordinate_missing:
            coordinate_note.style("display: none")
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
            {"name": "z_zero_position", "label": "Z zero", "required": True, "control": z_zero, "state_key": "z_zero_position", "section": "coordinate", "card": coordinate_card, "icon": coordinate_icon, "note": coordinate_note},
            {"name": "coordinate_system", "label": "Coordinate system", "required": True, "control": coordinate_system, "state_key": "coordinate_system", "section": "coordinate", "card": coordinate_card, "icon": coordinate_icon, "note": coordinate_note},
        ]
    )

    tools_card = ui.element("div").classes("input-card ok")
    with tools_card:
        tools_icon = _input_status_header("Tools", True)
        ui.label("Planner preference | source: planner.yaml and machine.yaml").classes("input-card-source")
        tool_options = {tool.id: f"{tool.id} - {tool.description}" for tool in machine.tools}
        tool_control = ui.select(
            options=tool_options,
            value=inputs.get("tools") or planner.defaults.default_tool,
            label="Tool",
        ).props("outlined dense").classes("w-full")
    widgets.append({"name": "tools", "label": "Tools", "required": False, "control": tool_control, "state_key": "tools", "section": "tools", "card": tools_card, "icon": tools_icon, "note": None})

    workholding_missing = not inputs.get("workholding_method")
    workholding_card = ui.element("div").classes("input-card" + (" missing" if workholding_missing else " ok"))
    with workholding_card:
        workholding_icon = _input_status_header("Workholding", not workholding_missing)
        ui.label(_sources_text(["workholding_method"], sources)).classes("input-card-source")
        workholding_note = ui.label("Missing required workholding method.").classes("missing-note")
        if not workholding_missing:
            workholding_note.style("display: none")
        workholding = ui.select(
            list(machine.machine.workholding),
            value=inputs.get("workholding_method") or [],
            label="Workholding",
            multiple=True,
        ).props("outlined dense use-chips").classes("w-full")
    widgets.append({"name": "workholding_method", "label": "Workholding", "required": True, "control": workholding, "state_key": "workholding_method", "section": "workholding", "card": workholding_card, "icon": workholding_icon, "note": workholding_note})
    return widgets


def _input_status_header(title: str, ok: bool):
    with ui.element("div").classes("input-card-title-row"):
        icon = ui.icon("check_circle" if ok else "cancel").classes("input-status-ok" if ok else "input-status-missing")
        ui.label(title).classes("input-card-title")
    return icon


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


def _update_input_section_status(widgets: list[dict[str, Any]]) -> None:
    sections: dict[str, list[dict[str, Any]]] = {}
    for widget in widgets:
        sections.setdefault(widget.get("section", widget["name"]), []).append(widget)
    for section_widgets in sections.values():
        required = [widget for widget in section_widgets if widget["required"]]
        ok = all(_widget_has_value(widget["control"]) for widget in required)
        card = section_widgets[0].get("card")
        icon = section_widgets[0].get("icon")
        note = section_widgets[0].get("note")
        if card is not None:
            card.classes(replace="input-card ok" if ok else "input-card missing")
        if icon is not None:
            icon.name = "check_circle" if ok else "cancel"
            icon.classes(replace="input-status-ok" if ok else "input-status-missing")
        if note is not None:
            note.style("display: none" if ok else "")


def _planning_request(
    machine: MachineFile,
    planner: PlannerFile,
    artifacts: ProjectArtifacts,
    state: dict[str, Any],
) -> PlanningRequest:
    inputs = state["inputs"]
    return PlanningRequest.model_validate(
        {
            "geometry": artifacts.geometry,
            "machine": machine.model_dump(mode="json"),
            "system_advice": load_system_planner_advice().model_dump(mode="json"),
            "user_advice": planner.operation_advice.model_dump(mode="json"),
            "fixed_dxf": artifacts.fixed_dxf.read_text(encoding="utf-8", errors="ignore"),
            "inputs": {
                "stock_xy": inputs.get("stock_xy"),
                "stock_units": inputs.get("stock_units"),
                "stock_thickness": _float_or_none(inputs.get("stock_thickness")),
                "stock_material": inputs.get("stock_material"),
                "z_zero_position": inputs.get("z_zero_position"),
                "coordinate_system": inputs.get("coordinate_system"),
                "workholding_method": inputs.get("workholding_method") or [],
                "tools": inputs.get("tools"),
                "planning_notes": inputs.get("planning_notes"),
                "finishing_allowance": planner.defaults.finishing_allowance,
                "cut_deeper_than_stock": planner.defaults.cut_deeper_than_stock,
                "screw_spacing": planner.defaults.screw_spacing,
            },
        }
    )


def _issues_yaml(response) -> str:
    from io import StringIO

    from ruamel.yaml import YAML

    data = response.model_dump(mode="json", exclude_none=True)
    buffer = StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.dump(data, buffer)
    return buffer.getvalue()


async def _post_plan_request(request: PlanningRequest) -> PlanningResponse:
    return await asyncio.to_thread(generate_operation_plan, request)


async def _post_toolpath_request(request: ToolpathRequest) -> ToolpathResponse:
    return await asyncio.to_thread(_post_toolpath_request_sync, request)


def _post_toolpath_request_sync(request: ToolpathRequest) -> ToolpathResponse:
    port = int(os.environ.get("DXFWIZ_PORT", "8080"))
    body = json.dumps(request.model_dump(mode="json")).encode("utf-8")
    http_request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/toolpaths",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(http_request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    return ToolpathResponse.model_validate(data)


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
        toolpaths_class = "nav-active" if active == "toolpaths" else ("nav-link" if state.get("inputs", {}).get("op_yaml") else "nav-muted")
        toolpaths_button = ui.button(
            "toolpaths",
            icon="route",
            on_click=lambda: render_toolpaths_state(state["content"], state),
        ).props("flat dense").classes(toolpaths_class)
        if not state.get("inputs", {}).get("op_yaml"):
            toolpaths_button.disable()


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
    logger.info("Received DXF upload: %s (%d bytes)", filename, len(upload_bytes))
    try:
        artifacts = service.create_project_from_upload(filename, upload_bytes)
    except Exception as exc:  # pragma: no cover - surfaced in UI
        logger.exception("Failed to process DXF upload: %s", filename)
        ui.notify(f"Failed to process DXF: {exc}", type="negative")
        return

    state["project"] = artifacts
    ui.notify("geom.yaml generated", type="positive")
    logger.info("Generated geometry for project %s", artifacts.project_id)
    render_project_state(content_host, state, machine, artifacts)


def render_job_display(artifacts: ProjectArtifacts, state: dict[str, Any] | None = None) -> str:
    geometry = _geometry_from_state(artifacts, state)
    generated_entities = _generated_entities_from_geometry(geometry)
    svg = _job_scene_svg(artifacts.geometry_svg, geometry, generated_entities)
    return f"""
    <div class="dxf-viewer">
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
      #dxfwiz-scene #legend-layer {{ display: none; }}
      #dxfwiz-scene #workholding-layer .workholding-outline {{
        fill: rgba(251, 146, 60, 0.12);
        stroke: #f97316;
        stroke-width: 2px;
        vector-effect: non-scaling-stroke;
      }}
      #dxfwiz-scene #workholding-layer .workholding-label {{
        fill: #f97316;
        font-family: Segoe UI, Arial, sans-serif;
        font-weight: 800;
        paint-order: stroke;
        stroke: #ffffff;
        stroke-width: 0.06px;
        vector-effect: non-scaling-stroke;
      }}
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


def _viewer_javascript() -> str:
    return """
      window.dxfwizResetView = function() {
        const svg = document.getElementById('dxfwiz-scene');
        if (!svg || !svg.dataset.initialViewBox) return;
        svg.setAttribute('viewBox', svg.dataset.initialViewBox);
        svg.dataset.currentViewBox = svg.dataset.initialViewBox;
        window.dxfwizUpdateLabelScale();
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
        window.dxfwizUpdateLabelScale();
        svg.focus();
      };
      window.dxfwizRotate = function(delta) {
        const svg = document.getElementById('dxfwiz-scene');
        if (!svg) return;
        const next = ((Number(svg.dataset.rotation || '0') + delta) % 360 + 360) % 360;
        svg.dataset.rotation = String(next);
        const vb = (svg.dataset.currentViewBox || svg.getAttribute('viewBox')).split(' ').map(Number);
        const cx = vb[0] + vb[2] / 2;
        const cy = vb[1] + vb[3] / 2;
        svg.querySelectorAll('#geometry-layer, #annotation-layer, #workholding-layer').forEach(layer => {
          layer.setAttribute('transform', `rotate(${next} ${cx} ${cy})`);
        });
        svg.querySelectorAll('.entity-name-text, .diameter-label, .workholding-label').forEach(text => {
          const x = text.getAttribute('x') || '0';
          const y = text.getAttribute('y') || '0';
          text.setAttribute('transform', `rotate(${-next} ${x} ${y})`);
        });
      };
      window.dxfwizToggleLayer = function(layerName) {
        const svg = document.getElementById('dxfwiz-scene');
        if (!svg) return;
        const className = layerName === 'entity-labels' ? 'hide-entity-labels' : 'hide-dimension-labels';
        const hidden = svg.classList.toggle(className);
        if (document.activeElement) {
          document.activeElement.classList.toggle('toggle-on', !hidden);
        }
      };
      window.dxfwizHighlightEntities = function(entityIds) {
        const svg = document.getElementById('dxfwiz-scene');
        if (!svg) return;
        svg.querySelectorAll('.selected-entity').forEach(element => {
          element.classList.remove('selected-entity');
        });
        const wanted = new Set((entityIds || []).map(entityId => String(entityId)));
        if (wanted.size === 0) {
          svg.focus();
          return;
        }
        svg.querySelectorAll('[data-entity-id], [data-entity-ref]').forEach(element => {
          const entityId = element.dataset.entityId || element.dataset.entityRef;
          if (wanted.has(String(entityId))) {
            element.classList.add('selected-entity');
          }
        });
        svg.focus();
      };
      window.dxfwizUpdateLabelScale = function() {
        const svg = document.getElementById('dxfwiz-scene');
        if (!svg || !svg.dataset.initialViewBox || !svg.dataset.currentViewBox) return;
        const initial = svg.dataset.initialViewBox.split(' ').map(Number);
        const current = svg.dataset.currentViewBox.split(' ').map(Number);
        if (!initial[2] || !current[2]) return;
        const factor = Math.max(0.28, Math.min(1.75, current[2] / initial[2]));
        svg.querySelectorAll('.entity-name-text, .workholding-label').forEach(text => {
          if (!text.dataset.baseFontSize) {
            const raw = text.getAttribute('font-size') || window.getComputedStyle(text).fontSize || '';
            text.dataset.baseFontSize = String(parseFloat(raw) || 1);
          }
          text.setAttribute('font-size', `${Number(text.dataset.baseFontSize) * factor}px`);
        });
      };
      window.dxfwizShowOperationYaml = function(yamlText, entityIds) {
        window.dxfwizHighlightEntities(entityIds);
        let panel = document.getElementById('operation-yaml-popover');
        if (!panel) {
          panel = document.createElement('div');
          panel.id = 'operation-yaml-popover';
          panel.className = 'operation-yaml-popover';
          document.body.appendChild(panel);
        }
        panel.replaceChildren();
        const header = document.createElement('header');
        const title = document.createElement('span');
        title.textContent = 'Operation YAML';
        const close = document.createElement('button');
        close.type = 'button';
        close.setAttribute('aria-label', 'Dismiss');
        close.textContent = 'x';
        close.addEventListener('click', function() { panel.remove(); });
        const pre = document.createElement('pre');
        pre.textContent = yamlText || '';
        header.appendChild(title);
        header.appendChild(close);
        panel.appendChild(header);
        panel.appendChild(pre);
      };

      window.dxfwizInitSplitter = function() {
        document.querySelectorAll('.toolpaths-grid').forEach(grid => {
          const splitter = grid.querySelector('.splitter');
          if (!splitter || splitter.dataset.ready === '1') return;
          splitter.dataset.ready = '1';
          splitter.addEventListener('pointerdown', function(event) {
            event.preventDefault();
            splitter.classList.add('dragging');
            const rect = grid.getBoundingClientRect();
            function move(moveEvent) {
              const rightWidth = Math.min(Math.max(rect.right - moveEvent.clientX, 320), rect.width * 0.72);
              grid.style.gridTemplateColumns = `minmax(0, 1fr) 7px ${rightWidth}px`;
            }
            function stop() {
              splitter.classList.remove('dragging');
              window.removeEventListener('pointermove', move);
              window.removeEventListener('pointerup', stop);
            }
            window.addEventListener('pointermove', move);
            window.addEventListener('pointerup', stop);
          });
        });
      };

      window.dxfwizInitViewer = function() {
        const svg = document.getElementById('dxfwiz-scene');
        if (!svg || svg.dataset.dxfwizReady === '1') return;
        svg.dataset.dxfwizReady = '1';
        const initial = svg.dataset.initialViewBox || svg.getAttribute('viewBox');
        svg.setAttribute('viewBox', initial);
        svg.dataset.initialViewBox = initial;
        svg.dataset.currentViewBox = initial;
        svg.dataset.rotation = svg.dataset.rotation || '0';
        let dragging = false;
        let last = null;
        function current() { return svg.dataset.currentViewBox.split(' ').map(Number); }
        function setViewBox(vb) {
          svg.dataset.currentViewBox = vb.join(' ');
          svg.setAttribute('viewBox', svg.dataset.currentViewBox);
          window.dxfwizUpdateLabelScale();
        }
        svg.addEventListener('selectstart', function(event) { event.preventDefault(); });
        svg.addEventListener('focus', function() { svg.dataset.zoomActive = '1'; });
        svg.addEventListener('wheel', function(event) {
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
      window.dxfwizInstallViewerHooks = function() {
        window.dxfwizInitViewer();
        window.dxfwizUpdateLabelScale();
        window.dxfwizInitSplitter();
        if (window.dxfwizMutationObserver || !document.body) return;
        window.dxfwizMutationObserver = new MutationObserver(() => {
          window.dxfwizInitViewer();
          window.dxfwizInitSplitter();
        });
        window.dxfwizMutationObserver.observe(document.body, { childList: true, subtree: true });
      };
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', window.dxfwizInstallViewerHooks);
      } else {
        window.dxfwizInstallViewerHooks();
      }
    """


def _viewer_action_javascript(action: str) -> str:
    return f"{_viewer_javascript()}\n{action}"


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


def _geometry_from_state(artifacts: ProjectArtifacts, state: dict[str, Any] | None) -> dict[str, Any]:
    if state and state.get("geometry"):
        return state["geometry"]
    return artifacts.geometry


def _generated_entities_from_geometry(geometry: dict[str, Any]) -> list[dict[str, Any]]:
    return geometry.get("generated_entities", [])


def _write_project_geometry(artifacts: ProjectArtifacts, geometry: dict[str, Any]) -> None:
    try:
        dump_yaml_file(artifacts.geom_yaml, geometry)
        logger.info("Updated geom.yaml for project %s with generated geometry", artifacts.project_id)
    except Exception:
        logger.exception("Failed to update geom.yaml for project %s", artifacts.project_id)


def _op_yaml_path(artifacts: ProjectArtifacts) -> Path:
    return artifacts.geom_yaml.with_name("op.yaml")


def _gcode_path(artifacts: ProjectArtifacts) -> Path:
    return artifacts.geom_yaml.with_name("toolpaths.nc")


def _write_project_op_yaml(artifacts: ProjectArtifacts, op_yaml: str) -> None:
    if not op_yaml:
        return
    try:
        _op_yaml_path(artifacts).write_text(op_yaml, encoding="utf-8")
        logger.info("Wrote op.yaml for project %s", artifacts.project_id)
    except Exception:
        logger.exception("Failed to write op.yaml for project %s", artifacts.project_id)


def _write_project_gcode(artifacts: ProjectArtifacts, gcode: str) -> None:
    if not gcode:
        return
    try:
        _gcode_path(artifacts).write_text(gcode, encoding="utf-8")
        logger.info("Wrote toolpaths.nc for project %s", artifacts.project_id)
    except Exception:
        logger.exception("Failed to write toolpaths.nc for project %s", artifacts.project_id)


def _job_scene_svg(
    svg_path: Path,
    geometry: dict[str, Any],
    generated_entities: list[dict[str, Any]] | None = None,
) -> str:
    svg = svg_path.read_text(encoding="utf-8")
    if generated_entities:
        svg = svg.replace("</svg>", _workholding_layer(generated_entities, geometry) + "\n</svg>")
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


def _workholding_layer(generated_entities: list[dict[str, Any]], geometry: dict[str, Any]) -> str:
    bounds = geometry["summary"]["bounding_box"]
    max_y = bounds["max"]["y"]
    width = max(bounds["max"]["x"] - bounds["min"]["x"], 1e-6)
    height = max(bounds["max"]["y"] - bounds["min"]["y"], 1e-6)
    marker_radius = min(max(max(width, height) / 420, 0.035), 0.095)
    font_size = marker_radius * 3.4
    elements = ['<g id="workholding-layer">']
    for entity in generated_entities:
        entity_id = escape(entity["id"])
        if entity["shape"] == "circle" and entity.get("center"):
            x = entity["center"]["x"]
            y = max_y - entity["center"]["y"]
            radius = (entity.get("diameter") or 0.1) / 2
            elements.append(f'<g class="workholding-entity" data-entity-ref="{entity_id}">')
            elements.append(
                f'<circle class="workholding-outline" data-entity-id="{entity_id}" cx="{x:.6f}" cy="{y:.6f}" r="{radius:.6f}" />'
            )
            elements.append(
                f'<text class="workholding-label" data-entity-ref="{entity_id}" x="{x + radius:.6f}" y="{y - radius:.6f}" font-size="{font_size:.6f}px">{entity_id}</text>'
            )
            elements.append("</g>")
        elif entity["shape"] == "rectangle" and entity.get("lower_left") and entity.get("upper_right"):
            x1 = entity["lower_left"]["x"]
            x2 = entity["upper_right"]["x"]
            y1 = max_y - entity["upper_right"]["y"]
            y2 = max_y - entity["lower_left"]["y"]
            elements.append(f'<g class="workholding-entity" data-entity-ref="{entity_id}">')
            if entity.get("center") and entity.get("width") and entity.get("height") and entity.get("angle_deg") is not None:
                cx = entity["center"]["x"]
                cy = max_y - entity["center"]["y"]
                rect_w = entity["width"]
                rect_h = entity["height"]
                angle = -entity["angle_deg"]
                elements.append(
                    f'<rect class="workholding-outline" data-entity-id="{entity_id}" '
                    f'x="{(cx - rect_w / 2):.6f}" y="{(cy - rect_h / 2):.6f}" '
                    f'width="{rect_w:.6f}" height="{rect_h:.6f}" '
                    f'transform="rotate({angle:.6f} {cx:.6f} {cy:.6f})" />'
                )
            else:
                elements.append(
                    f'<rect class="workholding-outline" data-entity-id="{entity_id}" x="{x1:.6f}" y="{y1:.6f}" width="{(x2 - x1):.6f}" height="{(y2 - y1):.6f}" />'
                )
            if entity.get("role") != "tab":
                elements.append(
                    f'<text class="workholding-label" data-entity-ref="{entity_id}" x="{x2:.6f}" y="{y1:.6f}" font-size="{font_size:.6f}px">{entity_id}</text>'
                )
            elements.append("</g>")
    elements.append("</g>")
    return "\n".join(elements)


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
