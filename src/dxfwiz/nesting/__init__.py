from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dxfwiz.nesting.extract_parts import ExtractedPart, ExtractPartsResult

__all__ = [
    "ExtractPartsResult",
    "ExtractedPart",
]


def __getattr__(name: str):
    if name in __all__:
        from importlib import import_module

        extract_parts_module = import_module("dxfwiz.nesting.extract_parts")
        return getattr(extract_parts_module, name)
    raise AttributeError(name)
