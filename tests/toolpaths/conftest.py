from __future__ import annotations

import re
from html import escape
from pathlib import Path


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "toolpaths"
COMBINED_PREVIEW = OUTPUT_DIR / "all_toolpath_previews.svg"


def pytest_sessionfinish(session, exitstatus) -> None:
    svg_files = sorted(
        path
        for path in OUTPUT_DIR.glob("*.svg")
        if path.name != COMBINED_PREVIEW.name
    )
    if svg_files:
        combine_svg_previews(svg_files, COMBINED_PREVIEW)


def combine_svg_previews(svg_files: list[Path], output_path: Path) -> None:
    sections = []
    width = 0
    y_offset = 0
    gap = 44
    for svg_file in svg_files:
        svg_text = svg_file.read_text(encoding="utf-8")
        parsed = _parse_svg(svg_text)
        if parsed is None:
            continue
        item_width, item_height, view_box, body = parsed
        width = max(width, item_width)
        sections.extend(
            [
                f'<text class="bundle-title" x="24" y="{y_offset + 28}">{escape(svg_file.name)}</text>',
                (
                    f'<svg x="0" y="{y_offset + gap}" width="{item_width}" height="{item_height}" '
                    f'viewBox="{escape(view_box)}">'
                ),
                body,
                "</svg>",
            ]
        )
        y_offset += item_height + gap + 28
    if not sections:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "\n".join(
            [
                (
                    f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{y_offset}" '
                    f'viewBox="0 0 {width} {y_offset}">'
                ),
                "<style>",
                ".bundle-title { font-family: Arial, sans-serif; font-size: 22px; font-weight: 700; fill: #111827; }",
                "</style>",
                '<rect x="0" y="0" width="100%" height="100%" fill="#ffffff" />',
                *sections,
                "</svg>",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _parse_svg(svg_text: str) -> tuple[int, int, str, str] | None:
    match = re.search(r"<svg\b(?P<attrs>[^>]*)>(?P<body>.*)</svg>\s*\Z", svg_text, re.DOTALL)
    if match is None:
        return None
    attrs = match.group("attrs")
    width = _svg_number_attr(attrs, "width")
    height = _svg_number_attr(attrs, "height")
    view_box = _svg_attr(attrs, "viewBox")
    if width is None or height is None:
        return None
    if view_box is None:
        view_box = f"0 0 {width} {height}"
    return width, height, view_box, match.group("body")


def _svg_number_attr(attrs: str, name: str) -> int | None:
    value = _svg_attr(attrs, name)
    if value is None:
        return None
    number = re.match(r"\d+(?:\.\d+)?", value)
    return int(float(number.group(0))) if number else None


def _svg_attr(attrs: str, name: str) -> str | None:
    match = re.search(rf'\b{name}="([^"]+)"', attrs)
    return match.group(1) if match else None
