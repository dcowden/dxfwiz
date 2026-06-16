from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Literal

from dxfwiz.gcode.lifter import CanonicalMove, CanonicalProgram, lift_gcode
from dxfwiz.toolpaths.model import (
    ArcMove,
    CoordinateSystemCommand,
    DistanceModeCommand,
    DwellMove,
    FeedModeCommand,
    LineMove,
    PlaneCommand,
    ProgramEndCommand,
    RapidMove,
    SpindleMove,
    SpindleSpeedCommand,
    ToolChangeMove,
    ToolpathCommand,
    ToolpathPlan,
    UnitsCommand,
)


@dataclass(frozen=True)
class GcodeValidationIssue:
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    line_no: int | None = None


def validate_gcode_against_plan(
    gcode: str,
    plan: ToolpathPlan,
    *,
    safe_z: float,
    tolerance: float = 1e-3,
) -> list[GcodeValidationIssue]:
    lifted = lift_gcode(gcode, tolerance=tolerance)
    expected = plan_to_canonical(plan)
    issues: list[GcodeValidationIssue] = []

    for warning in lifted.warnings:
        issues.append(GcodeValidationIssue("unsupported_or_invalid_gcode", warning, "error", _line_no_from_warning(warning)))

    if lifted.units != expected.units:
        issues.append(GcodeValidationIssue("units_mismatch", f"posted units {lifted.units!r} != plan units {expected.units!r}"))
    if lifted.coordinate_system != expected.coordinate_system:
        issues.append(
            GcodeValidationIssue(
                "coordinate_system_mismatch",
                f"posted coordinate system {lifted.coordinate_system!r} != plan coordinate system {expected.coordinate_system!r}",
            )
        )
    if lifted.distance_mode != "absolute":
        issues.append(GcodeValidationIssue("relative_mode", "posted G-code ends in relative distance mode"))
    if lifted.plane != "xy":
        issues.append(GcodeValidationIssue("unsupported_plane", "posted G-code is not in the XY plane"))

    issues.extend(_compare_moves(expected.moves, lifted.moves, tolerance))
    issues.extend(_safety_invariants(lifted.moves, safe_z=safe_z, tolerance=tolerance))
    return issues


def plan_to_canonical(plan: ToolpathPlan) -> CanonicalProgram:
    state = _PlanState()
    moves: list[CanonicalMove] = []
    for command in _plan_commands(plan):
        moves.extend(_canonical_from_command(command, state))
    return CanonicalProgram(
        units=state.units,
        distance_mode=state.distance_mode,
        coordinate_system=state.coordinate_system,
        plane=state.plane,
        feed_mode=state.feed_mode,
        moves=moves,
        warnings=[],
    )


@dataclass
class _PlanState:
    units: Literal["in", "mm"] | None = None
    distance_mode: Literal["absolute", "relative"] = "absolute"
    coordinate_system: str | None = None
    plane: Literal["xy", "unsupported"] = "xy"
    feed_mode: Literal["units_per_min", "unsupported"] = "units_per_min"
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    feed: float | None = None


def _plan_commands(plan: ToolpathPlan) -> Iterable[ToolpathCommand]:
    yield from plan.commands
    for toolpath_pass in plan.passes:
        yield from toolpath_pass.moves


def _canonical_from_command(command: ToolpathCommand, state: _PlanState) -> list[CanonicalMove]:
    if isinstance(command, UnitsCommand):
        state.units = command.length
        return []
    if isinstance(command, DistanceModeCommand):
        state.distance_mode = command.mode
        return []
    if isinstance(command, CoordinateSystemCommand):
        state.coordinate_system = command.code
        return []
    if isinstance(command, PlaneCommand):
        state.plane = "xy" if command.plane == "xy" else "unsupported"
        return []
    if isinstance(command, FeedModeCommand):
        state.feed_mode = "units_per_min" if command.mode == "units_per_min" else "unsupported"
        return []
    if isinstance(command, ToolChangeMove):
        return [CanonicalMove(kind="tool_change", tool=_tool_number_id(command.tool))]
    if isinstance(command, SpindleSpeedCommand):
        return [CanonicalMove(kind="spindle_speed", rpm=command.rpm)]
    if isinstance(command, SpindleMove):
        return [CanonicalMove(kind="spindle", spindle_on=command.state == "on")]
    if isinstance(command, DwellMove):
        return [CanonicalMove(kind="dwell", seconds=command.seconds)]
    if isinstance(command, ProgramEndCommand):
        return [CanonicalMove(kind="program_end")]
    if isinstance(command, RapidMove):
        start = state.position
        end = _command_endpoint(command.x, command.y, command.z, state)
        state.position = end
        if _same_xyz(start, end, 1e-12):
            return []
        return [CanonicalMove(kind="rapid", start=start, end=end)]
    if isinstance(command, LineMove):
        start = state.position
        end = _command_endpoint(command.x, command.y, command.z, state)
        state.position = end
        if command.feed is not None:
            state.feed = command.feed
        if _same_xyz(start, end, 1e-12):
            return []
        return [CanonicalMove(kind="line", start=start, end=end, feed=state.feed)]
    if isinstance(command, ArcMove):
        start = state.position
        end = _command_endpoint(command.x, command.y, command.z, state)
        state.position = end
        if command.feed is not None:
            state.feed = command.feed
        center = (start[0] + command.i, start[1] + command.j)
        radius = math.hypot(start[0] - center[0], start[1] - center[1])
        return [
            CanonicalMove(
                kind="arc",
                start=start,
                end=end,
                feed=state.feed,
                direction=command.direction,
                center=center,
                radius=radius,
            )
        ]
    return []


def _command_endpoint(x: float | None, y: float | None, z: float | None, state: _PlanState) -> tuple[float, float, float]:
    current_x, current_y, current_z = state.position
    if state.distance_mode == "relative":
        return current_x + (x or 0.0), current_y + (y or 0.0), current_z + (z or 0.0)
    return current_x if x is None else x, current_y if y is None else y, current_z if z is None else z


def _compare_moves(
    expected_moves: list[CanonicalMove],
    actual_moves: list[CanonicalMove],
    tolerance: float,
) -> list[GcodeValidationIssue]:
    issues: list[GcodeValidationIssue] = []
    if len(expected_moves) != len(actual_moves):
        issues.append(
            GcodeValidationIssue(
                "canonical_move_count_mismatch",
                f"expected {len(expected_moves)} canonical move(s), posted {len(actual_moves)}",
            )
        )
    for index, (expected, actual) in enumerate(zip(expected_moves, actual_moves, strict=False), start=1):
        if expected.kind != actual.kind:
            issues.append(
                GcodeValidationIssue(
                    "canonical_move_kind_mismatch",
                    f"move {index}: expected {expected.kind}, posted {actual.kind}",
                    line_no=actual.line_no or None,
                )
            )
            continue
        if expected.end is not None and actual.end is not None and not _same_xyz(expected.end, actual.end, tolerance):
            issues.append(
                GcodeValidationIssue(
                    "canonical_endpoint_mismatch",
                    f"move {index}: expected end {_fmt_xyz(expected.end)}, posted {_fmt_xyz(actual.end)}",
                    line_no=actual.line_no or None,
                )
            )
        if expected.start is not None and actual.start is not None and not _same_xyz(expected.start, actual.start, tolerance):
            issues.append(
                GcodeValidationIssue(
                    "canonical_startpoint_mismatch",
                    f"move {index}: expected start {_fmt_xyz(expected.start)}, posted {_fmt_xyz(actual.start)}",
                    line_no=actual.line_no or None,
                )
            )
        if expected.kind == "arc":
            if expected.direction != actual.direction:
                issues.append(
                    GcodeValidationIssue(
                        "canonical_arc_direction_mismatch",
                        f"move {index}: expected {expected.direction}, posted {actual.direction}",
                        line_no=actual.line_no or None,
                    )
                )
            if expected.center and actual.center and not _same_xy(expected.center, actual.center, tolerance):
                issues.append(
                    GcodeValidationIssue(
                        "canonical_arc_center_mismatch",
                        f"move {index}: expected center {_fmt_xy(expected.center)}, posted {_fmt_xy(actual.center)}",
                        line_no=actual.line_no or None,
                    )
                )
        if expected.kind == "tool_change" and expected.tool != actual.tool:
            issues.append(
                GcodeValidationIssue(
                    "canonical_tool_mismatch",
                    f"move {index}: expected tool {expected.tool}, posted {actual.tool}",
                    line_no=actual.line_no or None,
                )
            )
    return issues


def _safety_invariants(
    moves: list[CanonicalMove],
    *,
    safe_z: float,
    tolerance: float,
) -> list[GcodeValidationIssue]:
    issues: list[GcodeValidationIssue] = []
    saw_program_end = False
    for move in moves:
        if move.kind == "program_end":
            saw_program_end = True
        if move.kind == "rapid" and move.start is not None and move.end is not None:
            xy_delta = math.hypot(move.end[0] - move.start[0], move.end[1] - move.start[1])
            if xy_delta > tolerance and min(move.start[2], move.end[2]) < safe_z - tolerance:
                issues.append(
                    GcodeValidationIssue(
                        "rapid_xy_below_safe_z",
                        f"rapid XY move below safe Z {safe_z:.4f}: {_fmt_xyz(move.start)} -> {_fmt_xyz(move.end)}",
                        line_no=move.line_no or None,
                    )
                )
    if not saw_program_end:
        issues.append(GcodeValidationIssue("missing_program_end", "posted G-code has no M30 program end", "warning"))
    return issues


def _tool_number_id(tool_id: str) -> str:
    digits = "".join(ch for ch in str(tool_id) if ch.isdigit())
    return f"t{int(digits or '1')}"


def _same_xyz(first: tuple[float, float, float], second: tuple[float, float, float], tolerance: float) -> bool:
    return (
        abs(first[0] - second[0]) <= tolerance
        and abs(first[1] - second[1]) <= tolerance
        and abs(first[2] - second[2]) <= tolerance
    )


def _same_xy(first: tuple[float, float], second: tuple[float, float], tolerance: float) -> bool:
    return abs(first[0] - second[0]) <= tolerance and abs(first[1] - second[1]) <= tolerance


def _fmt_xyz(point: tuple[float, float, float]) -> str:
    return f"({point[0]:.4f}, {point[1]:.4f}, {point[2]:.4f})"


def _fmt_xy(point: tuple[float, float]) -> str:
    return f"({point[0]:.4f}, {point[1]:.4f})"


def _line_no_from_warning(warning: str) -> int | None:
    parts = warning.split(":", 1)[0].split()
    if len(parts) == 2 and parts[0] == "line":
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None
