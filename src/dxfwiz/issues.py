from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


IssueLevel = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class IssueDefinition:
    code: str
    level: IssueLevel
    title: str
    description: str

    @property
    def severity(self) -> int:
        return int(self.code[1])


ISSUES: dict[str, IssueDefinition] = {
    "W1001": IssueDefinition(
        code="W1001",
        level="warning",
        title="Additional planning notes not interpreted",
        description="The planner preserved user notes but did not fully interpret them.",
    ),
    "W1002": IssueDefinition(
        code="W1002",
        level="warning",
        title="Tabs require verification",
        description="Tab placement should be verified before cutting.",
    ),
    "W2001": IssueDefinition(
        code="W2001",
        level="warning",
        title="Tool change required",
        description="The selected tools exceed the machine automatic tool capacity.",
    ),
    "W2002": IssueDefinition(
        code="W2002",
        level="warning",
        title="Parts too close",
        description="Part spacing is smaller than the selected tool diameter.",
    ),
    "W2003": IssueDefinition(
        code="W2003",
        level="warning",
        title="Insufficient tab locations",
        description="The planner could not place the target number of tabs.",
    ),
    "W2004": IssueDefinition(
        code="W2004",
        level="warning",
        title="Interior slug remains",
        description="A helical contour operation may leave a large interior slug.",
    ),
    "W2005": IssueDefinition(
        code="W2005",
        level="warning",
        title="No operations generated",
        description="No machinable operations were generated from the geometry.",
    ),
    "W2006": IssueDefinition(
        code="W2006",
        level="warning",
        title="Planner repaired finishing settings",
        description="The planner normalized or repaired finishing settings.",
    ),
    "W2007": IssueDefinition(
        code="W2007",
        level="warning",
        title="Pocket not machinable",
        description="A pocket operation has no machinable area for the selected tool.",
    ),
    "W2008": IssueDefinition(
        code="W2008",
        level="warning",
        title="Roughing skipped for near-size feature",
        description="A roughing pass was skipped because only the finishing path fits the selected tool.",
    ),
    "W2009": IssueDefinition(
        code="W2009",
        level="warning",
        title="Invalid default tool replaced",
        description="The requested default tool was replaced with the largest tool that fits required features.",
    ),
    "W2010": IssueDefinition(
        code="W2010",
        level="warning",
        title="AI planner fell back to local planner",
        description="The AI planner failed and the deterministic local planner was used instead.",
    ),
    "W2011": IssueDefinition(
        code="W2011",
        level="warning",
        title="Excessive recutting",
        description="A material-removal simulation found dexels cut more than twice.",
    ),
    "W3001": IssueDefinition(
        code="W3001",
        level="warning",
        title="Unknown selected tool",
        description="A requested tool is not present in machine.yaml.",
    ),
    "E2001": IssueDefinition(
        code="E2001",
        level="error",
        title="Missing required planning input",
        description="Required planning data was not provided.",
    ),
    "E3001": IssueDefinition(
        code="E3001",
        level="error",
        title="Tool too large for feature",
        description="The selected tool cannot physically machine the referenced feature.",
    ),
    "E3002": IssueDefinition(
        code="E3002",
        level="error",
        title="Toolpath generation failed",
        description="Toolpath generation raised an exception.",
    ),
    "E3003": IssueDefinition(
        code="E3003",
        level="error",
        title="AI planner failed",
        description="The AI planner failed to return a usable plan.",
    ),
}


LEGACY_CODE_MAP = {
    "missing_required_input": "E2001",
    "unknown_tool": "W3001",
    "tool_change_required": "W2001",
    "tabs_not_fully_placed": "W1002",
    "notes_not_interpreted": "W1001",
    "parts_too_close": "W2002",
    "no_operations_generated": "W2005",
    "insufficient_tab_locations": "W2003",
    "separate_finish_contours_merged": "W2006",
    "contour_finishing_settings_repaired": "W2006",
    "roughing_skipped_to_fit": "W2008",
    "invalid_default_tool_replaced": "W2009",
    "ai_planner_fell_back_to_local": "W2010",
    "toolpath_generation_failed": "E3002",
    "missing_gemini_api_key": "E3003",
    "ai_planner_error": "E3003",
    "missing_ai_plan": "E3003",
}


def issue_code(code_or_legacy: str) -> str:
    return LEGACY_CODE_MAP.get(code_or_legacy, code_or_legacy)


def issue_severity(code: str) -> int:
    normalized = issue_code(code)
    if len(normalized) < 2 or not normalized[1].isdigit():
        return 0
    return int(normalized[1])


def classify_toolpath_warning(message: str) -> str:
    lowered = message.lower()
    if "too large for hole diameter" in lowered or "no machinable area" in lowered and "tool diameter" in lowered:
        return "E3001"
    if "skipped roughing pass" in lowered:
        return "W2008"
    if "helical contour leaves an interior slug" in lowered or "helical drilling leaves an interior slug" in lowered:
        return "W2004"
    if "pocket offset" in lowered and "leaves no machinable area" in lowered:
        return "W2007"
    return "W1002"
