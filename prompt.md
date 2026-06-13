# dxfwiz

dxfwiz is a browser-based workflow for turning messy 2D DXF/SVG files into CNC-router-ready job bundles. The core idea is that every step produces open, human-readable YAML so humans, scripts, and AI agents can inspect and edit the workflow without being locked into one GUI.

The initial target is 2.5D CNC routers, especially FRC-style workflows where the CAD student, CAM planner, and machine operator may be different people. The current focus is DXF cleanup, geometry recognition, visualization, and preparation for AI-assisted operation planning.

## Collaboration Model

dxfwiz is not designed around the assumption that one person owns the whole job from design through cutting. In FRC and many real shops, "the operator" is often a myth: the part designer, CAM planner, machine operator, mentor/reviewer, and person responsible for workholding may be different people with different knowledge and preferences.

The job is the shared object that passes between roles. Files in the bundle should make those handoffs explicit:

- designers should be able to mark geometry intent, part criticality, keepouts, and features that need special treatment
- CAM/planning users should be able to express machining preferences, strategy, tool choices, and acceptable tradeoffs
- machine operators should be able to express physical machine reality, setup defaults, available tools, workholding preferences, and manual-intervention limits
- reviewers should be able to inspect `geom.yaml`, `op.yaml`, toolpaths, warnings, and generated gcode without needing the original GUI state

This means YAML files are not just machine inputs; they are collaboration artifacts. They should remain human-readable, diffable, and role-friendly. A novice should not need to understand every advanced policy to complete a simple job, but the system should have clear places for each role to record its preferences when needed.

Role onboarding is part of the product. When a user first sets up as a designer, machine operator, CAM planner, or reviewer, the app should collect only the preferences relevant to that role. Those preferences can later contribute to a job without forcing every user to become an expert in every domain.

The long-term web app should support live collaboration: when one person changes geometry intent, operation inputs, tool policy, warnings, or plan review status, other collaborators should be able to see that change in the same job context.

## Current Flow

1. User uploads a DXF.
2. The system creates a project id and a project workspace folder.
3. The DXF is cleaned/fixed.
4. `geom.yaml` is generated from the fixed DXF.
5. A diagnostic SVG is generated from `geom.yaml` and the fixed DXF.
6. The UI displays the fixed geometry on top of the machine work area.
7. Later steps will collect machining intent, generate an operation plan, preview toolpaths, generate gcode, and bundle everything.

For now, the NiceGUI app stops after generating `geom.yaml`.

## YAML Files

### `machine.yaml`

Physical machine configuration only.

Includes:

- file-level units: length and speed
- machine name/type/axes
- work envelope
- coordinate system
- spindle details
- `max_tools`, where `1` means no tool changer and values greater than `1` mean a tool changer with that capacity
- allowed material workholding methods: `clamps`, `screws`, `tape`, `vacuum`
- allowed part-holding methods: `onionskin`, `z_rollers`, `z_presser`, `tabs`
- tool library

Tool feeds and speeds live on tools as defaults and are not material-specific. Operations can override them later.

Current local machine:

- units: inches and inches/minute
- 3-axis vertically oriented ER11 CNC router
- work envelope: X 0..96 in, Y 0..50 in, Z -3.15..0
- X positive right, Y positive up
- origin bottom-left
- typical tools: 1/8 flat 2-flute, 1/8 flat 1-flute, 3/16 compression, 3/16 upcut, 1/4 upcut 2-flute, 1/4 upcut 1-flute

### `post.yaml`

Post-processor/dialect configuration. Initial target is UCCNC.

This should eventually include all dialect details needed to emit valid gcode. Fusion 360 UCCNC post processors are useful references.

### `planner.yaml`

Planner guidance file. This replaces the earlier `job_template.yaml` name.

Contains operator/planner/CAM preferences that are not physical machine facts:

- defaults such as stock thickness, z-zero preference, coordinate system, and planner-level `max_tools`
- English `operation_advice` blocks:
  - `workholding`
  - `tools`
  - `geometry`

Over time, planner preferences should be able to come from role-specific defaults, job-specific overrides, and direct user instructions. The planner should merge those into a clear job-level intent instead of requiring every user to edit a fully detailed expert policy.

Do not use structured `auto_rules`; put guidance in English advice. Example geometry advice:

- for holes between 0.04 in and 0.2 in, prefer drill operations
- for holes above drill size, use `helical_contour` only when the remaining center plug is smaller than the machine `maximum_plug_size`; otherwise use `helical_pocket` up to the configured maximum circular-pocket diameter
- `operation_settings.prefer_arcs` controls controller compatibility: when true, circular/helical operations should emit arc moves where possible; when false, line-segment approximations are acceptable for compatibility-focused posts/controllers
- if a strict rectangular frame encloses the parts, treat it as stock/frame

Planner `max_tools` means the number of tools the user is willing to use for the job. This differs from machine `max_tools`: if the machine has `max_tools: 1` but planner says `max_tools: 2`, the user is willing to tolerate one manual tool change.

Tool-change policy should be presented in user-centered terms first, then mapped to structured settings. Useful examples:

- "least attention": prefer one tool and avoid surprise manual interventions
- "fastest runtime": allow tool changes when they materially reduce cutting time
- "one planned change after fixtures": allow a predictable tool change after fixture/screw operations, but avoid random mid-job changes
- "one tool only": force a single selected or best-fit tool unless the job is impossible

For manual/single-spindle machines, separate gcode files per tool should be treated as a first-class output mode, not merely a workaround. Many router users prefer this because it makes the manual tool-change moment explicit and inspectable.

### `operation_inputs.yaml`

Defines fixed required inputs that must be known before generating an operation plan. This list is not user-modifiable in ordinary planner files.

Required inputs:

- stock size
- stock material
- workholding method
- z-zero position
- coordinate system, such as G54 or G55

The planner should use machine config, planner config, and geometry to answer as many as possible. If anything remains unknown, ask the user before generating the plan.

### `geom.yaml`

Generated from the fixed vector file. This is meant to be easy for a human to fact-check.

Top-level order:

1. `schema_version`
2. `units`
3. `source`
4. `summary`
5. `entity_map`
6. `entities`

`summary` includes entity count, closed/open count, ignored entity count, and bounding box.

`entity_map` is the human-readable containment/role map. It appears before full entity definitions. Use compact map syntax for leaf nodes and omit `children` when there are no children:

```yaml
entity_map:
- entity: e9
  role: frame
  children:
  - entity: e1
    role: part
    children:
    - {entity: e10, role: cutout}
    - {entity: e11, role: cutout}
- {entity: e30, role: uncontained}
```

`entities` contains the full details for each entity:

- stable `id`
- type: `closed_loop` or `open_path`
- shape: `circle`, `rectangle`, `polyline`, `polyline_with_arcs`, etc.
- `source_refs` linking back to fixed DXF handles or dxfwiz ids
- bounding box
- center/diameter for circles
- area/perimeter where available

Entity IDs in `geom.yaml` must trace back to fixed DXF entities. The fixed DXF is considered the geometry source of record for linking.

Open paths are eligible for future operations. The operation name for following an open entity is `trace`.

### `job.yaml` / Future Operation Plan

The eventual operation file contains machining operations such as:

- drill
- helical contour
- helical pocket
- pocket
- contour
- trace

The order of operations should be the order in the file.

Typical operation options include:

- tool
- depth, where depth means negative Z into material
- climb/conventional direction
- stock allowance / finish pass
- ramp options
- tabs/onion skin/etc.
- feeds and speeds overrides

## DXF Cleaning

DXF cleanup is a known hard problem. The current implementation uses `ezdxf` and focuses on:

- reading LINE, ARC, CIRCLE, LWPOLYLINE, and old POLYLINE entities
- snapping endpoints by tolerance
- removing zero-length entities
- removing duplicate/redundant segments
- chaining segments into open or closed paths
- preserving circles and arcs where possible
- writing fixed DXFs that SolidWorks and LightBurn can open

Generated fixed DXFs should not change units from the source/fixed coordinate space. If unit guessing later determines the design is inch-sized despite mm-looking coordinates, `geom.yaml` can scale reported coordinates using `coordinate_scale`, but the fixed DXF should remain in its original coordinate values.

## Geometry Recognition

Use Shapely for containment/nesting.

Roles:

- `frame`
- `part`
- `cutout`
- `island`
- `outer_boundary`
- `hole_candidate`
- `uncontained`
- `ignored`

Frame detection is strict:

- A frame is only a rectangular closed loop with four straight sides.
- Frames usually represent stock size.
- If multiple frames exist and only one contains profiles, recognize only the populated rectangle as the frame.
- Empty top-level rectangular stock frames should be marked with role `ignored` when a populated frame also exists.
- Ignored entities remain listed in `geom.yaml` and count toward `summary.ignored_count`.
- If all top-level closed loops are rectangles and none contains profiles, treat them as parts, not frames.

Containment meaning:

- A frame contains parts.
- Parts contain cutouts.
- Cutouts may contain islands.
- Current real test files do not contain islands.

## Unit Detection

If the DXF contains trustworthy explicit units, use those units. If not, guess only between inches and millimeters.

Heuristics:

- router-scale plausibility
- no practical CNC-router feature should be smaller than about 0.01 in
- no practical part/job should be larger than about 9 ft for this use case
- common circular hole sizes
- common fractional inch sizes
- common metric hole sizes
- common stock/frame sizes
- weak DXF hints such as `$MEASUREMENT`

Some DXFs may declare or imply mm even when the design is actually inch-sized. `geom.yaml` should record:

- selected length unit
- source: `explicit_dxf` or `guessed`
- confidence
- coordinate scale
- evidence list

The three current real test DXFs should resolve to inches.

## SVG Diagnostics

The diagnostic SVG is part of the bundle and should be useful without JavaScript.

It should include layers:

- geometry layer
- annotation layer
- legend layer

Geometry display:

- part outlines are bold/dark
- cutouts/holes are lighter
- frame is green in the UI and diagnostic SVG
- ignored entities are hidden in the planner view, but may be shown faintly in diagnostic SVGs
- segment/node markers show where entities are split
- entity start/end markers are purple
- hover titles show entity id, DXF handle, type, shape, vertex count, arc count, etc.

Circle labels:

- repeated holes of the same diameter are grouped per part
- labels use drafting-style callouts outside the frame
- callout bubbles may be placed on either left or right side
- leaders have thin strokes and arrows
- leaders point to the nearest point on the referenced circle, not the center
- labels use the diameter symbol, e.g. `⌀ 0.201 in`, with count on a second line such as `12 places`

The SVG should be generated from `geom.yaml` and the fixed DXF.

## NiceGUI UI

The UI is a NiceGUI web app launched by:

```cmd
run.cmd
```

For now it uses `examples/machine.yaml` and `examples/planner.yaml`, but it should be designed so users can later maintain their own machine/planner files.

Current UI behavior:

- Initial state shows a centered "Choose a DXF" upload call to action.
- Do not show the machine SVG or chat until geometry is generated.
- Uploading a DXF creates a project id and stores artifacts under `workspace/<project_id>/`.
- After geometry generation, show:
  - the machine work area
  - generated geometry SVG as a layer
  - a chat panel stub
- The menu/wizard bar moves left to right:
  - choose dxf
  - uploaded file / geom.yaml
  - operation plan
  - toolpaths
- Prior wizard steps should be clickable where practical.
- Settings belong on the far right.

Viewer behavior:

- Mouse wheel zooms only after the user clicks/focuses the SVG viewer.
- Left click and drag pans.
- Zoom buttons use icons, not text.
- Include zoom in, zoom out, and zoom extents.
- Show machine origin coordinate axes:
  - +X red arrow
  - +Y green arrow

Chat behavior:

- Chat is hidden until geometry exists.
- Chat input should be obvious and fixed at the bottom of the chat panel.
- The large empty area should be the log, not a mysterious input area.
- Chat planning behavior is stubbed for now.

Operation planning behavior:

- The UI must call the same stateless FastAPI planning endpoint exposed to external clients.
- The planning endpoint accepts geometry, machine, system planner advice, user planner advice, and user inputs in the request body.
- The endpoint response always has the same structure: `errors`, `warnings`, `plan`, and `op_yaml`.
- `plan` may be empty/null when required inputs are missing or blocking planning errors exist.
- The first UI planning output is the generated `op.yaml` source for review and iteration.

System planner advice:

- Built-in planner assumptions live in `src/dxfwiz/planning/system_planner_advice.yaml`.
- User advice from `planner.yaml` is combined with system advice when generating a plan.
- System advice is not a source of missing inputs; it is used to guide plan generation and produce warnings/errors.

Planner issue philosophy:

- Errors block plan or toolpath generation until the user changes inputs, geometry, tools, or planner settings.
- Warnings indicate the system made a reasonable choice, but the user should review the result before cutting.
- Some errors can be converted into warnings by applying explicit planner fixups.
- Fixups must be configurable in `planner.yaml` under `defaults.fixups`.
- Each fixup has a slug-friendly name, an English description, and a boolean value.
- A fixup should only run when the code can prove the adjusted operation is still physically machinable.
- When a fixup is applied, emit a warning that names the original problem and the chosen fix.
- Example: if a selected tool can finish a circular hole but cannot leave the requested roughing allowance, the planner/toolpath generator may skip roughing for that hole and emit a warning such as "Skipped roughing pass to accommodate selected tool." If the tool still cannot fit at finishing diameter, keep the original error.

## Service Architecture

Use service objects for workflow steps. The current UI has a `ProjectService` that:

- creates a unique project id
- creates `workspace/<project_id>/`
- stores the uploaded original DXF
- writes fixed DXF
- writes `geom.yaml`
- writes diagnostic SVG

This local filesystem implementation is temporary. Design the boundary so later deployments can use:

- FastAPI endpoints
- background workers such as Celery or arq
- Redis/database state
- temporary server storage or object storage

## Bundle Contents

A bundle should include everything required to continue the job elsewhere:

- original DXF/SVG
- fixed DXF/SVG
- `geom.yaml`
- machine/planner/post files used or references to them
- future `op.yaml` / operation plan
- future generated gcode

## Toolpath Strategy

Do not casually hand-roll all toolpath generation. Pocketing, ramps, finishing passes, rest machining, tool diameter compensation, and efficient roughing are hard.

Current direction:

- Use SVG for display/diagnostics.
- Use Shapely and pyclipper/Clipper-style algorithms where appropriate.
- Use Kiri:Moto as a reference for toolpath behavior, especially pocketing and ramping.
- Be careful with licenses. GPL code used only behind a web app generally does not trigger distribution the way AGPL does, but this must be considered carefully if code is reused or ported.
- Use a NumPy-backed dexel simulator for material-removal testing. Internally, `actual_depth` is positive depth removed below stock top. The simulator must keep `expected_depth`, `actual_depth`, `cut_count`, and `last_operation_id`, and its request/response boundary must stay stateless so it can later run behind a separate service.
- Static simulator previews should be generated as output artifacts by tests, not by the service layer. Initial previews use top and isometric views; future UI work can reuse the same grid/snapshot data in Three.js.

## Testing

This is a professional project. Keep tests first-class.

Use:

- Python 3.13+
- pytest
- pyproject.toml
- editable package install

Test layout:

- source under `src/dxfwiz`
- tests under `tests`
- real and synthetic DXF integration fixtures under `tests/integration_tests/<case>/`
- each fixture case has its own `machine.yaml`
- generated integration outputs under `tests/output/<case>/`
- `tests/output/` is ignored by git

Current real fixture cases:

- `2xintake`
- `intakev4`
- `intake_frontv2`

Integration tests should:

- clean real DXFs
- write fixed DXFs
- write `geom.yaml`
- write SVG diagnostics
- validate geometry/entity counts against screenshot-derived expectations
- validate unit detection
- validate frame/part/cutout nesting
- validate UI service project artifact generation

Run tests with:

```cmd
run_tests.cmd
```

## Current Implementation Notes

Implemented so far:

- project skeleton with `pyproject.toml`
- schemas for machine, post, planner, operation inputs, job, geom
- DXF cleaner
- geometry extraction and containment mapping
- inch/mm unit guessing
- diagnostic SVG renderer
- integration fixtures and outputs
- compact `entity_map` in `geom.yaml`
- NiceGUI app through geometry-generation stage
- `ProjectService` local workspace flow
- `run.cmd`
- `run_tests.cmd`

Next major work:

- improve UI polish and interaction
- add operation-plan generation flow
- define `op.yaml`
- use machine + planner + geom + user chat to gather missing required operation inputs
- preview proposed operations/toolpaths
- choose/port/reference toolpath algorithms
- generate gcode through post processor
