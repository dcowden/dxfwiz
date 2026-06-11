from __future__ import annotations

from dataclasses import dataclass

from dxfwiz.schemas import GeometryFile
from dxfwiz.schemas.job import DrillOperation, HelicalDrillOperation, JobFile, PocketOperation
from dxfwiz.simulation.model import ExpectedRemoval


@dataclass(frozen=True)
class ExpectedRemovalBuildResult:
    removals: list[ExpectedRemoval]
    supported_operation_ids: set[str]
    warnings: list[str]


def build_expected_removals(job: JobFile, geometry: GeometryFile) -> ExpectedRemovalBuildResult:
    entities = {entity.id: entity for entity in geometry.entities}
    generated = {entity.id: entity for entity in job.generated_entities}
    removals: list[ExpectedRemoval] = []
    supported_operation_ids: set[str] = set()
    warnings: list[str] = []

    for operation in job.operations:
        if isinstance(operation, DrillOperation | HelicalDrillOperation):
            entity = entities.get(operation.entity) or generated.get(operation.entity)
            center = getattr(entity, "center", None) if entity is not None else None
            diameter = _operation_hole_diameter(operation, entity)
            if center is None or diameter is None:
                warnings.append(f"{operation.id}: could not build circular expected removal for {operation.entity}")
                continue
            removals.append(
                {
                    "type": "circle",
                    "operation_id": operation.id,
                    "entity": operation.entity,
                    "center_x": center.x,
                    "center_y": center.y,
                    "radius": diameter / 2,
                    "depth": operation.depth,
                }
            )
            supported_operation_ids.add(operation.id)
        elif isinstance(operation, PocketOperation):
            entity = entities.get(operation.entity)
            if entity is None or entity.shape != "rectangle" or entity.bounding_box is None:
                warnings.append(
                    f"{operation.id}: only rectangle pocket expected-removal is supported in this pass"
                )
                continue
            box = entity.bounding_box
            removals.append(
                {
                    "type": "rectangle",
                    "operation_id": operation.id,
                    "entity": operation.entity,
                    "min_x": box.min.x,
                    "min_y": box.min.y,
                    "max_x": box.max.x,
                    "max_y": box.max.y,
                    "depth": operation.depth,
                }
            )
            supported_operation_ids.add(operation.id)

    return ExpectedRemovalBuildResult(
        removals=removals,
        supported_operation_ids=supported_operation_ids,
        warnings=warnings,
    )


def _operation_hole_diameter(operation: DrillOperation | HelicalDrillOperation, entity) -> float | None:
    if isinstance(operation, HelicalDrillOperation) and operation.hole_diameter is not None:
        return operation.hole_diameter
    return getattr(entity, "diameter", None)

