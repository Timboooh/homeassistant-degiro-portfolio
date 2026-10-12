"""Shared fixtures."""

from collections.abc import Generator
from unittest.mock import patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow Home Assistant to load custom_components/ in every test."""
    yield


@pytest.fixture(autouse=True)
def _no_real_degiro_logout() -> Generator[None]:
    """Never let a test reach the real library by accident."""
    with patch("custom_components.degiro_portfolio.api.API") as api:
        api.side_effect = AssertionError("test tried to use the real Degiro API")
        yield
