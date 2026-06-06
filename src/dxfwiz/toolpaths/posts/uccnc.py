from __future__ import annotations

import logging

from dxfwiz.toolpaths.core import ToolpathCommand, ToolpathProgram


logger = logging.getLogger(__name__)


class UccncPost:
    def __init__(self, precision: int = 4) -> None:
        self.precision = precision

    def render(self, program: ToolpathProgram) -> str:
        logger.info("Rendering UCCNC G-code with %d command(s)", len(program.commands))
        lines = []
        for command in program.commands:
            line = self._render_command(command)
            if line:
                lines.append(line)
        return "\n".join(lines) + "\n"

    def _render_command(self, command: ToolpathCommand) -> str | None:
        values = command.values
        if command.name == "comment":
            return f"({values['text']})"
        if command.name == "units":
            return "G21" if values["length"] == "mm" else "G20"
        if command.name == "distance_mode":
            return "G90" if values["mode"] == "absolute" else "G91"
        if command.name == "plane":
            return "G17"
        if command.name == "feed_mode":
            return "G94"
        if command.name == "coordinate_system":
            return values["code"]
        if command.name == "tool_change":
            return f"T{_tool_number(values['tool'])} M6"
        if command.name == "spindle_on":
            return f"S{int(values['rpm'])} M3"
        if command.name == "spindle_off":
            return "M5"
        if command.name == "rapid":
            return self._motion("G0", values)
        if command.name == "feed":
            return self._motion("G1", values)
        if command.name == "arc":
            code = "G3" if values.get("direction") == "ccw" else "G2"
            return self._motion(code, values, include_ij=True)
        if command.name == "dwell":
            return f"G4 P{self._format(values['seconds'])}"
        if command.name == "program_end":
            return "M30"
        return None

    def _motion(self, code: str, values: dict, include_ij: bool = False) -> str:
        parts = [code]
        for axis in ("x", "y", "z"):
            if axis in values and values[axis] is not None:
                parts.append(axis.upper() + self._format(values[axis]))
        if include_ij:
            for axis in ("i", "j"):
                if axis in values and values[axis] is not None:
                    parts.append(axis.upper() + self._format(values[axis]))
        if "feed" in values and values["feed"] is not None:
            parts.append("F" + self._format(values["feed"]))
        return " ".join(parts)

    def _format(self, value: float) -> str:
        text = f"{float(value):.{self.precision}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text


def _tool_number(tool_id: str) -> int:
    digits = "".join(ch for ch in str(tool_id) if ch.isdigit())
    return int(digits or "1")
