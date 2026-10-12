"""End-to-end tests: setup, sensors, caching, fallbacks, button, staleness."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.degiro_portfolio.api import DegiroError, Quote, YahooClient, YahooError
from custom_components.degiro_portfolio.const import DOMAIN

ENTRY_ID = "test_entry"
STORE_KEY = f"{DOMAIN}.{ENTRY_ID}"
ENTRY_DATA = {
    "username": "user@example.com",
    "password": "secret",
    "totp_secret_key": "JBSWY3DPEHPK3PXP",
    "int_account": 123456,
}
COORD = "custom_components.degiro_portfolio.coordinator"

HOLDINGS = {
    "cash": 100.0,
    "positions": [
        {"product_id": 1, "name": "Microsoft", "isin": "US5949181045", "symbol": "MSFT",
         "currency": "USD", "qty": 2, "degiro_value": 700.0},
        {"product_id": 2, "name": "Vanguard S&P 500", "isin": "IE00B3XXRP09", "symbol": "VUSA",
         "currency": "GBX", "qty": 10, "degiro_value": 850.0},
        {"product_id": 3, "name": "Vanguard FTSE All-World", "isin": "IE00BK5BQT80", "symbol": "VWCE",
         "currency": "EUR", "qty": 12, "degiro_value": 1400.0},
    ],
}

# Yahoo returns prices in major currency units already (the client converts pence).
QUOTES = {
    "MSFT": Quote(350.0, "USD", "2026-10-09"),
    "VUSA.L": Quote(85.0, "GBP", "2026-10-09"),
    "VWCE.DE": Quote(116.5, "EUR", "2026-10-09"),
    "VWCE.L": Quote(101.0, "USD", "2026-10-09"),
    "EURUSD=X": Quote(1.10, "USD", "2026-10-09"),
    "EURGBP=X": Quote(0.85, "GBP", "2026-10-09"),
}
SEARCH = {
    "US5949181045": [{"symbol": "MSFT", "quoteType": "EQUITY"}],
    "IE00BK5BQT80": [
        {"symbol": "VWCE.L", "quoteType": "ETF"},  # wrong currency: must lose
        {"symbol": "VWCE.DE", "quoteType": "ETF"},
    ],
}

# MSFT 2*350/1.10, VUSA 10*85/0.85, VWCE 12*116.5
EXPECTED_INVESTMENTS = round(636.3636 + 1000.0 + 1398.0, 2)  # 3034.36
EXPECTED_TOTAL = round(EXPECTED_INVESTMENTS + 100.0, 2)  # 3134.36


async def fake_quote(symbol: str) -> Quote:
    if symbol not in QUOTES:
        raise YahooError(f"{symbol}: no data")
    return QUOTES[symbol]


async def fake_search(query: str) -> list[dict]:
    return SEARCH.get(query, [])


@pytest.fixture
def degiro():
    with patch(f"{COORD}.fetch_holdings", return_value=HOLDINGS) as mock:
        yield mock


@pytest.fixture
def yahoo():
    with (
        patch.object(YahooClient, "quote", new=AsyncMock(side_effect=fake_quote)) as q,
        patch.object(YahooClient, "search", new=AsyncMock(side_effect=fake_search)),
    ):
        yield q


def make_entry(**kwargs) -> MockConfigEntry:
    kwargs.setdefault("options", {"ticker_overrides": "IE00B3XXRP09=VUSA.L"})
    return MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        unique_id="user@example.com",
        entry_id=ENTRY_ID,
        **kwargs,
    )


def stored_holdings(age: timedelta) -> dict:
    return {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {
            "holdings": HOLDINGS,
            "holdings_updated_at": (dt_util.utcnow() - age).isoformat(),
        },
    }


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ------------------------------------------------------------------- sensors --


async def test_sensors_and_math(hass, hass_storage, degiro, yahoo) -> None:
    entry = make_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    value = hass.states.get("sensor.degiro_portfolio_value")
    assert value is not None, "entity id must stay sensor.degiro_portfolio_value"
    assert float(value.state) == pytest.approx(EXPECTED_TOTAL)
    attrs = value.attributes
    assert attrs["unit_of_measurement"] == "EUR"
    assert attrs["device_class"] == "monetary"
    assert attrs["state_class"] == "total"  # required for the change-over-time cards
    assert attrs["investments"] == pytest.approx(EXPECTED_INVESTMENTS)
    assert attrs["cash"] == 100.0
    assert attrs["stale"] is False
    assert attrs["fallback_positions"] == []
    by_name = {p["name"]: p for p in attrs["positions"]}
    assert by_name["Microsoft"]["value_eur"] == pytest.approx(636.36)
    assert by_name["Vanguard S&P 500"]["value_eur"] == pytest.approx(1000.0)  # pence + FX
    assert by_name["Vanguard S&P 500"]["ticker"] == "VUSA.L"  # from the override
    assert by_name["Vanguard FTSE All-World"]["ticker"] == "VWCE.DE"  # EUR listing wins

    inv = hass.states.get("sensor.degiro_portfolio_investments")
    cash = hass.states.get("sensor.degiro_portfolio_cash")
    assert float(inv.state) == pytest.approx(EXPECTED_INVESTMENTS)
    assert float(cash.state) == 100.0

    registry = er.async_get(hass)
    assert registry.async_get("sensor.degiro_portfolio_value").unique_id == f"{ENTRY_ID}_value"

    # Auto-resolved tickers are cached; the manual override is not.
    tickers = hass_storage[STORE_KEY]["data"]["tickers"]
    assert tickers == {"US5949181045": "MSFT", "IE00BK5BQT80": "VWCE.DE"}
    assert hass_storage[STORE_KEY]["data"]["holdings"] == HOLDINGS
    degiro.assert_called_once_with("user@example.com", "secret", "JBSWY3DPEHPK3PXP", 123456)


def test_positions_attribute_is_not_recorded() -> None:
    from custom_components.degiro_portfolio.sensor import DegiroSensor

    assert {"positions", "fallback_positions"} <= DegiroSensor._unrecorded_attributes


# ------------------------------------------------------------ Degiro caching --


async def test_fresh_cache_means_no_degiro_login(hass, hass_storage, degiro, yahoo) -> None:
    hass_storage[STORE_KEY] = stored_holdings(timedelta(hours=2))
    await setup_entry(hass, make_entry())
    degiro.assert_not_called()
    assert float(hass.states.get("sensor.degiro_portfolio_value").state) == pytest.approx(EXPECTED_TOTAL)


async def test_old_cache_triggers_degiro(hass, hass_storage, degiro, yahoo) -> None:
    hass_storage[STORE_KEY] = stored_holdings(timedelta(hours=25))
    await setup_entry(hass, make_entry())
    degiro.assert_called_once()


async def test_degiro_down_uses_cached_holdings(hass, hass_storage, yahoo) -> None:
    hass_storage[STORE_KEY] = stored_holdings(timedelta(hours=25))
    with patch(f"{COORD}.fetch_holdings", side_effect=DegiroError("cannot_connect", "down")) as m:
        entry = make_entry()
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    m.assert_called_once()
    assert float(hass.states.get("sensor.degiro_portfolio_value").state) == pytest.approx(EXPECTED_TOTAL)


async def test_degiro_down_without_cache_retries_setup(hass, hass_storage, yahoo) -> None:
    with patch(f"{COORD}.fetch_holdings", side_effect=DegiroError("maintenance", "m")):
        entry = make_entry()
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_bad_credentials_with_cache_starts_reauth_once(hass, hass_storage, yahoo) -> None:
    hass_storage[STORE_KEY] = stored_holdings(timedelta(hours=25))
    with patch(f"{COORD}.fetch_holdings", side_effect=DegiroError("invalid_auth", "bad")) as m:
        entry = make_entry()
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.LOADED  # keeps working on cached holdings
        flows = hass.config_entries.flow.async_progress()
        assert [f["context"]["source"] for f in flows] == ["reauth"]

        # Further refreshes must not retry the known-bad password.
        await entry.runtime_data.async_refresh_holdings()
        await hass.async_block_till_done()
        assert m.call_count == 1


async def test_bad_credentials_without_cache_fails_setup(hass, hass_storage, yahoo) -> None:
    with patch(f"{COORD}.fetch_holdings", side_effect=DegiroError("invalid_auth", "bad")):
        entry = make_entry()
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


# --------------------------------------------------------------------- Yahoo --


async def test_one_position_unpriced_falls_back_to_degiro(hass, hass_storage, degiro) -> None:
    quotes = {k: v for k, v in QUOTES.items() if k != "MSFT"}

    async def partial(symbol: str) -> Quote:
        if symbol not in quotes:
            raise YahooError("no data")
        return quotes[symbol]

    with (
        patch.object(YahooClient, "quote", new=AsyncMock(side_effect=partial)),
        patch.object(YahooClient, "search", new=AsyncMock(side_effect=fake_search)),
    ):
        await setup_entry(hass, make_entry())

    state = hass.states.get("sensor.degiro_portfolio_value")
    assert state.attributes["fallback_positions"] == ["Microsoft"]
    assert float(state.state) == pytest.approx(700.0 + 1000.0 + 1398.0 + 100.0)


async def test_nothing_priced_retries_setup(hass, hass_storage, degiro) -> None:
    with (
        patch.object(YahooClient, "quote", new=AsyncMock(side_effect=YahooError("429"))),
        patch.object(YahooClient, "search", new=AsyncMock(side_effect=YahooError("429"))),
    ):
        entry = make_entry()
        await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_wrong_override_is_used_verbatim(hass, hass_storage, degiro, yahoo) -> None:
    entry = make_entry(options={"ticker_overrides": "IE00BK5BQT80=VWCE.L\nIE00B3XXRP09=VUSA.L"})
    await setup_entry(hass, entry)
    by_name = {p["name"]: p for p in hass.states.get("sensor.degiro_portfolio_value").attributes["positions"]}
    assert by_name["Vanguard FTSE All-World"]["ticker"] == "VWCE.L"
    # 12 * 101 USD / 1.10
    assert by_name["Vanguard FTSE All-World"]["value_eur"] == pytest.approx(1101.82)


# -------------------------------------------------------------------- button --


async def test_button_forces_degiro_refresh(hass, hass_storage, degiro, yahoo) -> None:
    hass_storage[STORE_KEY] = stored_holdings(timedelta(hours=1))
    await setup_entry(hass, make_entry())
    degiro.assert_not_called()

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.degiro_portfolio_refresh_from_degiro"},
        blocking=True,
    )
    await hass.async_block_till_done()
    degiro.assert_called_once()


# ----------------------------------------------------------------- staleness --


async def test_stale_flag_after_two_days_of_failures(hass, hass_storage, yahoo, freezer) -> None:
    hass_storage[STORE_KEY] = stored_holdings(timedelta(hours=1))
    with patch(f"{COORD}.fetch_holdings", side_effect=DegiroError("cannot_connect", "down")):
        entry = make_entry()
        await setup_entry(hass, entry)
        assert hass.states.get("sensor.degiro_portfolio_value").attributes["stale"] is False

        # Yahoo dies and everything stays broken for ~2 days.
        yahoo.side_effect = YahooError("down")
        for _ in range(2):
            freezer.tick(timedelta(hours=25))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

    state = hass.states.get("sensor.degiro_portfolio_value")
    assert state.state != "unavailable", "sensors keep their last value"
    assert float(state.state) == pytest.approx(EXPECTED_TOTAL)
    assert state.attributes["stale"] is True
