"""Config flow, re-auth and options flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.degiro_portfolio.api import DegiroError
from custom_components.degiro_portfolio.const import DOMAIN

FLOW = "custom_components.degiro_portfolio.config_flow.validate_login"
SETUP = "custom_components.degiro_portfolio.async_setup_entry"


@pytest.fixture(autouse=True)
def no_setup():
    with patch(SETUP, return_value=True):
        yield


async def start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_success(hass: HomeAssistant) -> None:
    result = await start(hass)
    assert result["type"] is FlowResultType.FORM

    with patch(FLOW, return_value=987654) as validate:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": " user@example.com ", "password": "pw", "totp_secret_key": " ABC "},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Degiro (user@example.com)"
    assert result["data"] == {
        "username": "user@example.com",
        "password": "pw",
        "totp_secret_key": "ABC",
        "int_account": 987654,
    }
    validate.assert_called_once_with("user@example.com", "pw", " ABC ", None)


async def test_totp_is_optional(hass: HomeAssistant) -> None:
    result = await start(hass)
    with patch(FLOW, return_value=1):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "a", "password": "b"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "totp_secret_key" not in result["data"]


@pytest.mark.parametrize(
    "reason",
    ["invalid_auth", "totp_required", "app_approval", "captcha_required", "maintenance", "cannot_connect"],
)
async def test_user_flow_errors_then_recover(hass: HomeAssistant, reason: str) -> None:
    result = await start(hass)
    with patch(FLOW, side_effect=DegiroError(reason)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "a", "password": "b"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    with patch(FLOW, return_value=1):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "a", "password": "b"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_unexpected_error(hass: HomeAssistant) -> None:
    result = await start(hass)
    with patch(FLOW, side_effect=RuntimeError("boom")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "a", "password": "b"}
        )
    assert result["errors"] == {"base": "unknown"}


async def test_duplicate_account_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="user@example.com", data={}).add_to_hass(hass)
    result = await start(hass)
    with patch(FLOW, return_value=1):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "User@Example.com", "password": "b"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={"username": "user@example.com", "password": "old", "int_account": 5},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(FLOW, side_effect=DegiroError("invalid_auth")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "bad"})
    assert result["errors"] == {"base": "invalid_auth"}

    with patch(FLOW, return_value=5) as validate:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new", "totp_secret_key": "XYZ"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    validate.assert_called_once_with("user@example.com", "new", "XYZ", 5)
    assert entry.data["password"] == "new"
    assert entry.data["totp_secret_key"] == "XYZ"
    assert entry.data["username"] == "user@example.com"  # untouched


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="u", data={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"update_hours": 6, "ticker_overrides": "not a valid line"}
    )
    assert result["errors"] == {"ticker_overrides": "invalid_overrides"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"update_hours": 6, "ticker_overrides": "# comment\nie00bk5bqt80 = VWCE.DE\n"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "update_hours": 6,
        "ticker_overrides": "# comment\nie00bk5bqt80 = VWCE.DE\n",
    }
