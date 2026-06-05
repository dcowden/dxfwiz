from dxfwiz.logging_config import configure_logging


def pytest_configure() -> None:
    configure_logging()
