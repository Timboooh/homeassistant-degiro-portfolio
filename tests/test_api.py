"""Unit tests for the Degiro wrapper, the Yahoo client and the helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from degiro_connector.core.exceptions import (
    CaptchaRequiredError,
    DeGiroConnectionError,
    MaintenanceError,
)
from degiro_connector.trading.models.login import LoginError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.degiro_portfolio import api
from custom_components.degiro_portfolio.api import (
    DegiroError,
    YahooClient,
    YahooError,
    normalize_currency,
)
from custom_components.degiro_portfolio.coordinator import parse_ticker_overrides

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"


def chart_json(currency="EUR", price=116.5, ts=1760000000):
    return {
        "chart": {
            "result": [{"meta": {"currency": currency, "regularMarketPrice": price, "regularMarketTime": ts},
                        "indicators": {"quote": [{"close": [1.0, None, price]}]}}],
            "error": None,
        }
    }


# ----------------------------------------------------------------- helpers --


def test_normalize_currency() -> None:
    assert normalize_currency("GBp") == ("GBP", 100)
    assert normalize_currency("GBX") == ("GBP", 100)
    assert normalize_currency("USD") == ("USD", 1)
    assert normalize_currency(None) == (None, 1)


def test_parse_ticker_overrides() -> None:
    text = "# my overrides\n\nie00bk5bqt80 = VWCE.DE\nUS5949181045=MSFT\n"
    assert parse_ticker_overrides(text) == {"IE00BK5BQT80": "VWCE.DE", "US5949181045": "MSFT"}
    assert parse_ticker_overrides("") == {}
    for bad in ("VWCE.DE", "IE00BK5BQT80=", "NOTANISIN=VWCE.DE", "=VWCE.DE"):
        with pytest.raises(ValueError):
            parse_ticker_overrides(bad)


# ------------------------------------------------------------------- Yahoo --


async def test_quote_converts_pence(hass, aioclient_mock) -> None:
    aioclient_mock.get(CHART.format("VUSA.L"), json=chart_json("GBp", 8500.0))
    quote = await YahooClient(async_get_clientsession(hass)).quote("VUSA.L")
    assert (quote.price, quote.currency) == (85.0, "GBP")
    assert quote.as_of == "2025-10-09"


async def test_quote_fx_symbol_reaches_yahoo(hass, aioclient_mock) -> None:
    aioclient_mock.get(CHART.format("EURUSD=X"), json=chart_json("USD", 1.1))
    quote = await YahooClient(async_get_clientsession(hass)).quote("EURUSD=X")
    assert quote.price == 1.1
    assert aioclient_mock.call_count == 1


async def test_quote_falls_back_to_last_close(hass, aioclient_mock) -> None:
    data = chart_json("EUR", None)
    data["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [1.0, 2.5, None]
    aioclient_mock.get(CHART.format("X"), json=data)
    quote = await YahooClient(async_get_clientsession(hass)).quote("X")
    assert quote.price == 2.5


async def test_quote_errors(hass, aioclient_mock) -> None:
    client = YahooClient(async_get_clientsession(hass))
    aioclient_mock.get(CHART.format("RATE"), status=429)
    aioclient_mock.get(CHART.format("GONE"), json={"chart": {"result": None, "error": {"description": "No data found"}}})
    aioclient_mock.get(CHART.format("EMPTY"), json={"chart": {"result": [{"meta": {}}], "error": None}})
    aioclient_mock.get(CHART.format("JUNK"), text="<html>blocked</html>")
    for symbol, fragment in (("RATE", "429"), ("GONE", "No data"), ("EMPTY", "no price"), ("JUNK", "failed")):
        with pytest.raises(YahooError, match=fragment):
            await client.quote(symbol)


async def test_search(hass, aioclient_mock) -> None:
    aioclient_mock.get(
        "https://query2.finance.yahoo.com/v1/finance/search",
        json={"quotes": [{"symbol": "VWCE.DE", "quoteType": "ETF"}]},
    )
    assert await YahooClient(async_get_clientsession(hass)).search("IE00BK5BQT80") == [
        {"symbol": "VWCE.DE", "quoteType": "ETF"}
    ]


# ------------------------------------------------------------------ Degiro --


def row(**fields):
    return {"value": [{"name": k, "value": v} for k, v in fields.items()]}


class FakeApi:
    def __init__(self, update=None, info=None, details=None):
        self.credentials = None  # set by the factory below
        self._update, self._info, self._details = update, info, details
        self.logged_out = False
        self.connect_error = None

    def connect(self):
        if self.connect_error:
            raise self.connect_error

    def get_client_details(self):
        return self._details

    def get_update(self, request_list, raw):
        return self._update

    def get_products_info(self, product_list, raw):
        return self._info

    def logout(self):
        self.logged_out = True


UPDATE = {
    "portfolio": {"value": [
        row(id="1", positionType="PRODUCT", size=3, price=100, value=300, breakEvenPrice=90),
        row(id="2", positionType="PRODUCT", size=0, price=1, value=0),  # closed
        row(id="EUR", positionType="CASH", size=5, value=5.0),
    ]},
    "totalPortfolio": {"value": [{"name": "totalCash", "value": 12.3}]},
}
INFO = {"data": {"1": {"name": "X ETF", "isin": "IE000", "symbol": "X", "currency": "EUR"}}}


def patched_api(fake):
    """Patch API(...) so the fake receives the same Credentials object, like the real one."""

    def factory(credentials):
        fake.credentials = credentials
        return fake

    return patch.object(api, "API", side_effect=factory)


def run_fetch(fake):
    with patched_api(fake):
        return api.fetch_holdings("u", "p", "T", 42)


def test_fetch_holdings_parses_and_logs_out() -> None:
    fake = FakeApi(UPDATE, INFO)
    out = run_fetch(fake)
    assert out["cash"] == 12.3
    assert out["positions"] == [{
        "product_id": 1, "name": "X ETF", "isin": "IE000", "symbol": "X", "currency": "EUR",
        "qty": 3, "break_even_price": 90, "degiro_price": 100, "degiro_value": 300,
    }]
    assert fake.logged_out


def test_cash_falls_back_to_cash_rows() -> None:
    update = {**UPDATE, "totalPortfolio": {"value": []}}
    assert run_fetch(FakeApi(update, INFO))["cash"] == 5.0


def test_empty_responses_are_errors_and_still_log_out() -> None:
    fake = FakeApi(None, INFO)
    with pytest.raises(DegiroError) as err:
        run_fetch(fake)
    assert err.value.reason == "cannot_connect" and fake.logged_out
    with pytest.raises(DegiroError):
        run_fetch(FakeApi(UPDATE, None))


def test_account_number_is_discovered() -> None:
    fake = FakeApi(details={"data": {"intAccount": 777}})
    with patched_api(fake):
        assert api.validate_login("u", "p", None, None) == 777
    assert fake.logged_out


def test_missing_account_number_is_an_error() -> None:
    with patched_api(FakeApi(details={"data": {}})):
        with pytest.raises(DegiroError) as err:
            api.validate_login("u", "p", None, None)
    assert err.value.reason == "cannot_connect"


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (DeGiroConnectionError("2FA", LoginError(status=6)), "totp_required"),
        (DeGiroConnectionError("app", LoginError(status=12)), "app_approval"),
        (DeGiroConnectionError("bad", LoginError(status=3, status_text="badCredentials")), "invalid_auth"),
        (DeGiroConnectionError("No session id returned.", None), "cannot_connect"),
        (CaptchaRequiredError("captcha", LoginError(status=3, captcha_required=True)), "captcha_required"),
        (MaintenanceError("maint", LoginError(status=405)), "maintenance"),
    ],
)
def test_login_errors_are_mapped(exc, reason) -> None:
    fake = FakeApi(details={"data": {"intAccount": 1}})
    fake.connect_error = exc
    with patched_api(fake):
        with pytest.raises(DegiroError) as err:
            api.validate_login("u", "p", None, None)
    assert err.value.reason == reason


def test_auth_reasons_are_what_triggers_reauth() -> None:
    assert api.AUTH_REASONS == {"invalid_auth", "totp_required"}
