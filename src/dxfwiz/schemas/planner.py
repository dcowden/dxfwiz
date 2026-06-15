from typing import Literal

from pydantic import Field

from dxfwiz.schemas.common import StrictModel, Units
from dxfwiz.schemas.machine import PartHoldingOption, WorkholdingOption


class PlannerInfo(StrictModel):
    name: str
    description: str | None = None


class StockDefaults(StrictModel):
    material: str
    thickness: float = Field(gt=0)
    z_zero: Literal["stock_top", "spoilboard_top"]


class TabSettings(StrictModel):
    enabled: bool
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    spacing: float | None = Field(default=None, gt=0)


class ArcDetectionSettings(StrictModel):
    mode: Literal["OFF", "FOR_PLANNING", "RECOVER"] = "FOR_PLANNING"
    tolerance: float = Field(default=0.002, ge=0)


class OriginSettings(StrictModel):
    reorient_to_origin: bool = False


class FixupSetting(StrictModel):
    enabled: bool
    description: str


class Fixups(StrictModel):
    replace_invalid_default_tool: FixupSetting = Field(
        default_factory=lambda: FixupSetting(
            enabled=True,
            description="If the requested default tool cannot fit required features, choose the largest fitting tool and warn.",
        )
    )
    normalize_contour_finishing: FixupSetting = Field(
        default_factory=lambda: FixupSetting(
            enabled=True,
            description="Merge or repair separate/missing contour finishing settings into the contour operation.",
        )
    )
    accept_reduced_tab_count: FixupSetting = Field(
        default_factory=lambda: FixupSetting(
            enabled=True,
            description="If target tab count cannot be placed on straight segments, use the tabs that can be placed and warn.",
        )
    )
    skip_roughing_when_finish_fits: FixupSetting = Field(
        default_factory=lambda: FixupSetting(
            enabled=True,
            description="If roughing allowance makes a near-size hole impossible, skip roughing when the selected tool can still finish the feature.",
        )
    )
    fall_back_to_local_planner: FixupSetting = Field(
        default_factory=lambda: FixupSetting(
            enabled=False,
            description="If AI planning fails, use deterministic local planner and warn the user.",
        )
    )
    downgrade_pocket_to_profile_when_tool_fits_boundary: FixupSetting = Field(
        default_factory=lambda: FixupSetting(
            enabled=False,
            description="If a pocket cannot be cleared but its boundary can be profiled, convert to an inside contour and warn.",
        )
    )


class SimulationDefaults(StrictModel):
    enabled: bool = True
    xy_spacing: float | None = Field(default=None, gt=0)
    xy_tool_fraction: float = Field(default=0.5, gt=0, le=1)
    z_spacing: float | None = Field(default=None, gt=0)
    max_grid_cells: int = Field(default=20_000_000, gt=0)
    preview: bool = True
    arc_chord_fraction: float = Field(default=0.5, gt=0, le=2)


class OperationSettings(StrictModel):
    drill_max_diameter: float = Field(default=0.21, gt=0)
    helical_pocket_max_diameter: float = Field(default=2.0, gt=0)
    helical_drill_max_diameter: float | None = Field(default=None, gt=0)
    pocket_stepover_percent: float = Field(default=40.0, gt=0, le=100)
    prefer_arcs: bool = True


class Defaults(StrictModel):
    max_tools: int | None = Field(default=None, ge=1)
    coordinate_system: str | None = None
    stock: StockDefaults | None = None
    origin_location: Literal["bottom_left", "bottom_right", "top_left", "top_right", "center"]
    workholding: list[WorkholdingOption] = Field(default_factory=list)
    part_holding: list[PartHoldingOption] = Field(default_factory=list)
    default_tool: str | None = None
    milling_direction: Literal["climb", "conventional"]
    finishing_allowance: float = Field(ge=0)
    cut_deeper_than_stock: float = Field(default=0.0, ge=0)
    screw_spacing: float | None = Field(default=None, gt=0)
    ideal_screw_distance: float | None = Field(default=None, gt=0)
    min_screw_distance: float | None = Field(default=None, gt=0)
    operation_settings: OperationSettings = Field(default_factory=OperationSettings)
    arc_detection: ArcDetectionSettings = Field(default_factory=ArcDetectionSettings)
    origin: OriginSettings = Field(default_factory=OriginSettings)
    fixups: Fixups = Field(default_factory=Fixups)
    simulation: SimulationDefaults = Field(default_factory=SimulationDefaults)
    tab_settings: TabSettings


class OperationAdvice(StrictModel):
    overall: list[str] = Field(default_factory=list)
    workholding: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    geometry: list[str] = Field(default_factory=list)
    strategies: list[str] = Field(default_factory=list)


class PlannerFile(StrictModel):
    schema_version: str
    units: Units
    planner: PlannerInfo
    defaults: Defaults
    operation_advice: OperationAdvice
