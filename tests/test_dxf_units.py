from dxfwiz.dxf.units import determine_length_units


def test_explicit_dxf_inches_are_authoritative():
    decision = determine_length_units(
        doc_units=1,
        measurement=None,
        entities=[],
        containment_tree=[],
    )

    assert decision.length == "in"
    assert decision.source == "explicit_dxf"
    assert decision.confidence == 1.0


def test_explicit_dxf_millimeters_can_be_overridden_by_imperial_geometry():
    decision = determine_length_units(
        doc_units=4,
        measurement=None,
        entities=[
            {
                "id": "e1",
                "shape": "rectangle",
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 609.6, "y": 1219.2}},
            },
            {"id": "e2", "shape": "circle", "diameter": 5.105},
            {"id": "e3", "shape": "circle", "diameter": 6.35},
            {"id": "e4", "shape": "circle", "diameter": 9.525},
        ],
        containment_tree=[{"entity": "e1", "role": "frame", "children": []}],
    )

    assert decision.length == "in"
    assert decision.source == "guessed"
    assert decision.coordinate_scale == 1 / 25.4


def test_unitless_large_router_file_guesses_millimeters():
    decision = determine_length_units(
        doc_units=0,
        measurement=None,
        entities=[
            {
                "id": "e1",
                "shape": "rectangle",
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 600, "y": 1200}},
            },
            {"id": "e2", "shape": "circle", "diameter": 6.0},
            {"id": "e3", "shape": "circle", "diameter": 8.0},
        ],
        containment_tree=[{"entity": "e1", "role": "frame", "children": []}],
    )

    assert decision.length == "mm"
    assert decision.source == "guessed"
    assert decision.confidence > 0.55
    assert decision.coordinate_scale == 1.0


def test_unitless_fractional_holes_guess_inches():
    decision = determine_length_units(
        doc_units=0,
        measurement=None,
        entities=[
            {
                "id": "e1",
                "shape": "rectangle",
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 12, "y": 24}},
            },
            {"id": "e2", "shape": "circle", "diameter": 0.125},
            {"id": "e3", "shape": "circle", "diameter": 0.25},
            {"id": "e4", "shape": "circle", "diameter": 0.375},
        ],
        containment_tree=[{"entity": "e1", "role": "frame", "children": []}],
    )

    assert decision.length == "in"
    assert decision.source == "guessed"
    assert decision.confidence > 0.8
    assert decision.coordinate_scale == 1.0


def test_mm_coordinates_with_imperial_holes_guess_inches_with_scale():
    decision = determine_length_units(
        doc_units=4,
        measurement=None,
        entities=[
            {
                "id": "e1",
                "shape": "rectangle",
                "bounding_box": {"min": {"x": 0, "y": 0}, "max": {"x": 609.6, "y": 1219.2}},
            },
            {"id": "e2", "shape": "circle", "diameter": 5.105},
            {"id": "e3", "shape": "circle", "diameter": 6.35},
            {"id": "e4", "shape": "circle", "diameter": 9.525},
        ],
        containment_tree=[{"entity": "e1", "role": "frame", "children": []}],
    )

    assert decision.length == "in"
    assert decision.source == "guessed"
    assert decision.coordinate_scale == 1 / 25.4
