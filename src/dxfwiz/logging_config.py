from __future__ import annotations

import logging
import sys


DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging(level: int | str = logging.INFO) -> None:
    """Configure console logging for the application.

    Libraries should not configure global logging at import time, so this is called
    by entry points such as the NiceGUI app. Individual modules use
    ``logging.getLogger(__name__)`` so diagnostics can be enabled selectively later.
    """

    logging.basicConfig(
        level=level,
        format=DEFAULT_LOG_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("dxfwiz").setLevel(level)

    # ezdxf is useful when debugging import problems, but its handle-validation
    # chatter can swamp the console for messy CAD exports.
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
