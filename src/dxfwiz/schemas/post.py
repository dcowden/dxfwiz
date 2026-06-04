from typing import Literal

from dxfwiz.schemas.common import StrictModel, Units


class Formatting(StrictModel):
    line_numbers: bool
    line_number_start: int
    line_number_increment: int
    decimal_places: int
    comment_style: Literal["parentheses", "semicolon"]
    block_delete: str | None = None
    spaces_between_words: bool = True


class Motion(StrictModel):
    positioning_mode: Literal["absolute", "incremental"]
    arc_mode: Literal["incremental_ij", "absolute_ij"]
    arc_plane: str
    rapid: str
    linear: str
    cw_arc: str
    ccw_arc: str


class SpindleCommands(StrictModel):
    cw: str
    ccw: str
    stop: str
    speed_word: str


class CoolantCommands(StrictModel):
    flood_on: str | None = None
    mist_on: str | None = None
    off: str


class ToolChange(StrictModel):
    command: str
    tool_word: str
    length_comp: str | None = None
    length_comp_cancel: str | None = None
    pre_change_position: str | None = None
    manual_pause: bool


class Program(StrictModel):
    start_codes: list[str]
    end_codes: list[str]
    optional_stop: str | None = None
    program_end: str


class Safety(StrictModel):
    retract_mode: str
    safe_z: float
    feed_rate_mode: Literal["units_per_min"]


class Post(StrictModel):
    name: str
    description: str
    file_extension: str
    formatting: Formatting
    motion: Motion
    spindle: SpindleCommands
    coolant: CoolantCommands
    tool_change: ToolChange
    program: Program
    safety: Safety


class PostFile(StrictModel):
    schema_version: str
    units: Units
    post: Post

