from typing import Literal

from dxfwiz.schemas.common import StrictModel


InputName = Literal[
    "stock_size",
    "stock_material",
    "workholding_method",
    "z_zero_position",
    "coordinate_system",
]


class OperationInput(StrictModel):
    name: InputName
    description: str
    sources: list[Literal["machine", "planner", "geom", "user"]]
    required: bool = True


class OperationInputsFile(StrictModel):
    schema_version: str
    inputs: list[OperationInput]
