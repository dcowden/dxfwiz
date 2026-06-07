from dxfwiz.schemas.job import MoveOperation
from dxfwiz.toolpaths.moves import move_operation_to_toolpaths


def test_move_operation_generates_rapid_move():
    operation = MoveOperation.model_validate(
        {
            "id": "move-safe",
            "type": "move",
            "x": 1.0,
            "y": 2.0,
            "z": 0.5,
            "is_rapid": True,
        }
    )

    passes = move_operation_to_toolpaths(operation)

    assert len(passes) == 1
    assert passes[0].kind == "move"
    assert passes[0].tool is None
    assert passes[0].moves[0].type == "rapid"
    assert passes[0].moves[0].x == 1.0
    assert passes[0].moves[0].y == 2.0
    assert passes[0].moves[0].z == 0.5


def test_move_operation_generates_feed_move():
    operation = MoveOperation.model_validate(
        {
            "id": "move-feed",
            "type": "move",
            "x": 1.0,
            "y": 2.0,
            "z": -0.05,
            "is_rapid": False,
            "feed_rate": 12.0,
        }
    )

    passes = move_operation_to_toolpaths(operation)

    assert passes[0].moves[0].type == "line"
    assert passes[0].moves[0].feed == 12.0
