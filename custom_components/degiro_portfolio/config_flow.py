"""Config flow for the Degiro Portfolio integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import DegiroError, validate_login
from .const import (
    CONF_INT_ACCOUNT,
    CONF_TICKER_OVERRIDES,
    CONF_TOTP_SECRET,
    CONF_UPDATE_HOURS,
    DEFAULT_UPDATE_HOURS,
    DOMAIN,
    MAX_UPDATE_HOURS,
    MIN_UPDATE_HOURS,
)
from .coordinator import parse_ticker_overrides

_LOGGER = logging.getLogger(__name__)

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): _PASSWORD,
        vol.Optional(CONF_TOTP_SECRET): _PASSWORD,
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): _PASSWORD,
        vol.Optional(CONF_TOTP_SECRET): _PASSWORD,
    }
)


class DegiroConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the setup form."""

    VERSION = 1

    async def _async_validate(
        self,
        username: str,
        password: str,
        totp: str | None,
        int_account: int | None = None,
    ) -> tuple[int | None, str | None]:
        """Return (account number, None) on success or (None, error key)."""
        try:
            account = await self.hass.async_add_executor_job(
                validate_login, username, password, totp or None, int_account
            )
        except DegiroError as err:
            _LOGGER.debug("Degiro login check failed: %s", err)
            return None, err.reason
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error while checking the Degiro login")
            return None, "unknown"
        return account, None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()

            account, error = await self._async_validate(
                username, user_input[CONF_PASSWORD], user_input.get(CONF_TOTP_SECRET)
            )
            if error is None:
                data = {
                    CONF_USERNAME: username,
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_INT_ACCOUNT: account,
                }
                if user_input.get(CONF_TOTP_SECRET):
                    data[CONF_TOTP_SECRET] = user_input[CONF_TOTP_SECRET].strip()
                return self.async_create_entry(title=f"Degiro ({username})", data=data)
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            _, error = await self._async_validate(
                entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input.get(CONF_TOTP_SECRET),
                entry.data.get(CONF_INT_ACCOUNT),
            )
            if error is None:
                updates: dict[str, Any] = {CONF_PASSWORD: user_input[CONF_PASSWORD]}
                if user_input.get(CONF_TOTP_SECRET):
                    updates[CONF_TOTP_SECRET] = user_input[CONF_TOTP_SECRET].strip()
                return self.async_update_reload_and_abort(entry, data_updates=updates)
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(REAUTH_SCHEMA, user_input),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DegiroOptionsFlow:
        return DegiroOptionsFlow()


class DegiroOptionsFlow(OptionsFlow):
    """Refresh interval and ticker overrides."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self.config_entry.options
        if user_input is not None:
            try:
                parse_ticker_overrides(user_input.get(CONF_TICKER_OVERRIDES, ""))
            except ValueError:
                errors[CONF_TICKER_OVERRIDES] = "invalid_overrides"
            else:
                return self.async_create_entry(
                    data={
                        CONF_UPDATE_HOURS: int(user_input[CONF_UPDATE_HOURS]),
                        CONF_TICKER_OVERRIDES: user_input.get(CONF_TICKER_OVERRIDES, ""),
                    }
                )
            current = user_input

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_HOURS,
                    default=current.get(CONF_UPDATE_HOURS, DEFAULT_UPDATE_HOURS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_UPDATE_HOURS,
                        max=MAX_UPDATE_HOURS,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="h",
                    )
                ),
                vol.Optional(
                    CONF_TICKER_OVERRIDES,
                    default=current.get(CONF_TICKER_OVERRIDES, ""),
                ): TextSelector(TextSelectorConfig(multiline=True)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
