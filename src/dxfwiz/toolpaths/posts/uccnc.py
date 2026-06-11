from __future__ import annotations

import logging
from typing import Any

from dxfwiz.toolpaths.model import (
    ArcMove,
    CommentCommand,
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
    WarningCommand,
    UnitsCommand,
)


logger = logging.getLogger(__name__)


class UccncPost:
    def __init__(self, precision: int = 4) -> None:
        self.precision = precision

    def render(self, plan: ToolpathPlan) -> str:
        command_count = len(plan.commands) + sum(len(toolpath_pass.moves) for toolpath_pass in plan.passes)
        logger.info("Rendering UCCNC G-code with %d neutral command(s)", command_count)
        lines = []
        for command in plan.commands:
            line = self._render_command(command)
            if line:
                lines.append(line)
        for warning in plan.warnings:
            lines.append(f"(WARNING: {warning})")
        for toolpath_pass in plan.passes:
            for command in toolpath_pass.moves:
                line = self._render_command(command)
                if line:
                    lines.append(line)
        return "\n".join(lines) + "\n"

    def _render_command(self, command: ToolpathCommand) -> str | None:
        if isinstance(command, CommentCommand):
            return f"({command.text})"
        if isinstance(command, WarningCommand):
            return f"(WARNING: {command.text})"
        if isinstance(command, UnitsCommand):
            return "G21" if command.length == "mm" else "G20"
        if isinstance(command, DistanceModeCommand):
            return "G90" if command.mode == "absolute" else "G91"
        if isinstance(command, PlaneCommand):
            return "G17"
        if isinstance(command, FeedModeCommand):
            return "G94"
        if isinstance(command, CoordinateSystemCommand):
            return command.code
        if isinstance(command, ToolChangeMove):
            return f"T{_tool_number(command.tool)} M6"
        if isinstance(command, SpindleSpeedCommand):
            return f"S{int(command.rpm)}"
        if isinstance(command, SpindleMove):
            return "M3" if command.state == "on" else "M5"
        if isinstance(command, RapidMove):
            return self._motion("G0", command)
        if isinstance(command, LineMove):
            return self._motion("G1", command)
        if isinstance(command, ArcMove):
            return self._motion("G3" if command.direction == "ccw" else "G2", command, include_ij=True)
        if isinstance(command, DwellMove):
            return f"G4 P{self._format(command.seconds)}"
        if isinstance(command, ProgramEndCommand):
            return "M30"
        return None

    def _motion(self, code: str, command: Any, include_ij: bool = False) -> str:
        parts = [code]
        for axis in ("x", "y", "z"):
            value = getattr(command, axis, None)
            if value is not None:
                parts.append(axis.upper() + self._format(value))
        if include_ij:
            for axis in ("i", "j"):
                value = getattr(command, axis, None)
                if value is not None:
                    parts.append(axis.upper() + self._format(value))
        feed = getattr(command, "feed", None)
        if feed is not None:
            parts.append("F" + self._format(feed))
        return " ".join(parts)

    def _format(self, value: float) -> str:
        text = f"{float(value):.{self.precision}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text


def _tool_number(tool_id: str) -> int:
    digits = "".join(ch for ch in str(tool_id) if ch.isdigit())
    return int(digits or "1")
