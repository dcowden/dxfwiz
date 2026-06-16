from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Literal


MoveKind = Literal["rapid", "line", "arc", "dwell", "tool_change", "spindle", "spindle_speed", "program_end"]
ArcDirection = Literal["cw", "ccw"]


@dataclass(frozen=True)
class CanonicalMove:
    kind: MoveKind
    start: tuple[float, float, float] | None = None
    end: tuple[float, float, float] | None = None
    feed: float | None = None
    direction: ArcDirection | None = None
    center: tuple[float, float] | None = None
    radius: float | None = None
    tool: str | None = None
    rpm: int | None = None
    spindle_on: bool | None = None
    seconds: float | None = None
    line_no: int = 0
    raw: str = ""


@dataclass(frozen=True)
class CanonicalProgram:
    units: Literal["in", "mm"] | None
    distance_mode: Literal["absolute", "relative"]
    coordinate_system: str | None
    plane: Literal["xy", "unsupported"]
    feed_mode: Literal["units_per_min", "unsupported"]
    moves: list[CanonicalMove] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_TOKEN_RE = re.compile(r"([A-Z])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE)
_PAREN_COMMENT_RE = re.compile(r"\([^)]*\)")


def lift_gcode(gcode: str, *, tolerance: float = 1e-4) -> CanonicalProgram:
    """Lift a mill G-code subset into absolute typed moves.

    The lifter is deliberately conservative: unsupported modal groups are kept
    as warnings instead of being silently accepted. The supported subset matches
    what the current UCCNC post emits and common post-processor motion output.
    """
    state = _State()
    moves: list[CanonicalMove] = []
    warnings: list[str] = []

    for line_no, raw_line in enumerate(gcode.splitlines(), start=1):
        line = _strip_comments(raw_line).strip().upper()
        if not line:
            continue
        tokens = _tokens_by_letter(line)
        if not tokens:
            continue

        for code in tokens.get("G", []):
            int_code = _int_code(code)
            if int_code in {0, 1, 2, 3}:
                state.motion = int_code
            elif int_code == 4:
                seconds = _last(tokens, "P") or 0.0
                moves.append(CanonicalMove(kind="dwell", seconds=seconds, line_no=line_no, raw=raw_line))
            elif int_code == 17:
                state.plane = "xy"
            elif int_code in {18, 19}:
                state.plane = "unsupported"
                warnings.append(f"line {line_no}: unsupported plane G{int_code}")
            elif int_code == 20:
                state.units = "in"
            elif int_code == 21:
                state.units = "mm"
            elif int_code == 90:
                state.distance_mode = "absolute"
            elif int_code == 91:
                state.distance_mode = "relative"
            elif int_code == 94:
                state.feed_mode = "units_per_min"
            elif 54 <= int_code <= 59:
                state.coordinate_system = f"G{int_code}"
            else:
                warnings.append(f"line {line_no}: unsupported G{_format_code(code)}")

        if "F" in tokens:
            state.feed = tokens["F"][-1]
        if "S" in tokens:
            rpm = int(round(tokens["S"][-1]))
            state.rpm = rpm
            moves.append(CanonicalMove(kind="spindle_speed", rpm=rpm, line_no=line_no, raw=raw_line))
        if "T" in tokens:
            state.tool = f"t{int(round(tokens['T'][-1]))}"
            if any(_int_code(code) == 6 for code in tokens.get("M", [])):
                moves.append(CanonicalMove(kind="tool_change", tool=state.tool, line_no=line_no, raw=raw_line))

        for code in tokens.get("M", []):
            int_code = _int_code(code)
            if int_code == 3:
                state.spindle_on = True
                moves.append(CanonicalMove(kind="spindle", spindle_on=True, line_no=line_no, raw=raw_line))
            elif int_code == 5:
                state.spindle_on = False
                moves.append(CanonicalMove(kind="spindle", spindle_on=False, line_no=line_no, raw=raw_line))
            elif int_code == 6:
                if "T" not in tokens:
                    moves.append(CanonicalMove(kind="tool_change", tool=state.tool, line_no=line_no, raw=raw_line))
            elif int_code == 30:
                moves.append(CanonicalMove(kind="program_end", line_no=line_no, raw=raw_line))
            else:
                warnings.append(f"line {line_no}: unsupported M{_format_code(code)}")

        has_axis = any(axis in tokens for axis in ("X", "Y", "Z"))
        has_full_circle_arc = state.motion in {2, 3} and any(axis in tokens for axis in ("I", "J"))
        if (has_axis or has_full_circle_arc) and state.motion in {0, 1, 2, 3}:
            start = state.position
            end = _endpoint(state, tokens)
            state.position = end
            if state.motion == 0:
                if _same_xyz(start, end, 1e-12):
                    continue
                moves.append(CanonicalMove(kind="rapid", start=start, end=end, line_no=line_no, raw=raw_line))
            elif state.motion == 1:
                if _same_xyz(start, end, 1e-12):
                    continue
                moves.append(CanonicalMove(kind="line", start=start, end=end, feed=state.feed, line_no=line_no, raw=raw_line))
            else:
                i = _last(tokens, "I") or 0.0
                j = _last(tokens, "J") or 0.0
                center = (start[0] + i, start[1] + j)
                start_radius = math.hypot(start[0] - center[0], start[1] - center[1])
                end_radius = math.hypot(end[0] - center[0], end[1] - center[1])
                if abs(start_radius - end_radius) > tolerance:
                    warnings.append(
                        f"line {line_no}: arc radius mismatch start {start_radius:.6f} end {end_radius:.6f}"
                    )
                moves.append(
                    CanonicalMove(
                        kind="arc",
                        start=start,
                        end=end,
                        feed=state.feed,
                        direction="cw" if state.motion == 2 else "ccw",
                        center=center,
                        radius=start_radius,
                        line_no=line_no,
                        raw=raw_line,
                    )
                )

    return CanonicalProgram(
        units=state.units,
        distance_mode=state.distance_mode,
        coordinate_system=state.coordinate_system,
        plane=state.plane,
        feed_mode=state.feed_mode,
        moves=moves,
        warnings=warnings,
    )


@dataclass
class _State:
    units: Literal["in", "mm"] | None = None
    distance_mode: Literal["absolute", "relative"] = "absolute"
    coordinate_system: str | None = None
    plane: Literal["xy", "unsupported"] = "xy"
    feed_mode: Literal["units_per_min", "unsupported"] = "units_per_min"
    motion: int | None = None
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    feed: float | None = None
    tool: str | None = None
    rpm: int | None = None
    spindle_on: bool | None = None


def _endpoint(state: _State, tokens: dict[str, list[float]]) -> tuple[float, float, float]:
    x, y, z = state.position
    values = [_last(tokens, axis) for axis in ("X", "Y", "Z")]
    if state.distance_mode == "relative":
        return (
            x + (values[0] or 0.0),
            y + (values[1] or 0.0),
            z + (values[2] or 0.0),
        )
    return (
        x if values[0] is None else values[0],
        y if values[1] is None else values[1],
        z if values[2] is None else values[2],
    )


def _strip_comments(line: str) -> str:
    return _PAREN_COMMENT_RE.sub(" ", line.split(";", 1)[0])


def _tokens_by_letter(line: str) -> dict[str, list[float]]:
    tokens: dict[str, list[float]] = {}
    for letter, value in _TOKEN_RE.findall(line):
        tokens.setdefault(letter.upper(), []).append(float(value))
    return tokens


def _last(tokens: dict[str, list[float]], letter: str) -> float | None:
    values = tokens.get(letter)
    return values[-1] if values else None


def _int_code(code: float) -> int:
    return int(round(code))


def _format_code(code: float) -> str:
    return str(int(code)) if abs(code - int(code)) <= 1e-9 else str(code)


def _same_xyz(first: tuple[float, float, float], second: tuple[float, float, float], tolerance: float) -> bool:
    return (
        abs(first[0] - second[0]) <= tolerance
        and abs(first[1] - second[1]) <= tolerance
        and abs(first[2] - second[2]) <= tolerance
    )
