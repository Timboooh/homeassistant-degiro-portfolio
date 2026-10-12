"""Clients for Degiro (unofficial, blocking) and Yahoo Finance (async)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
import yfinance as yf
from degiro_connector.core.exceptions import (
    CaptchaRequiredError,
    DeGiroConnectionError,
    MaintenanceError,
)
from degiro_connector.trading.api import API
from degiro_connector.trading.models.account import UpdateOption, UpdateRequest
from degiro_connector.trading.models.credentials import Credentials
from homeassistant.core import HomeAssistant
from yfinance.exceptions import YFRateLimitError

_LOGGER = logging.getLogger(__name__)

# The library logs the Degiro session id at INFO level on every login; keep it
# out of Home Assistant's log even if someone raises the log level.
logging.getLogger("degiro_connector").setLevel(logging.WARNING)

# Reasons that mean "the stored credentials no longer work" (start re-auth).
AUTH_REASONS = frozenset({"invalid_auth", "totp_required"})

# Yahoo quotes some markets in minor units (pence, etc.).
MINOR_UNITS: dict[str, tuple[str, int]] = {
    "GBp": ("GBP", 100),
    "GBX": ("GBP", 100),
    "ZAc": ("ZAR", 100),
    "ILA": ("ILS", 100),
}


def normalize_currency(currency: str | None) -> tuple[str | None, int]:
    """Return (major currency code, divisor), e.g. 'GBp' -> ('GBP', 100)."""
    if currency is None:
        return None, 1
    return MINOR_UNITS.get(currency, (currency, 1))


# ------------------------------------------------------------------ Degiro --


class DegiroError(Exception):
    """A Degiro problem. `reason` doubles as the config-flow error key."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


def _map_connect_error(err: DeGiroConnectionError) -> DegiroError:
    if isinstance(err, MaintenanceError):
        return DegiroError("maintenance", str(err))
    if isinstance(err, CaptchaRequiredError):
        return DegiroError("captcha_required", str(err))
    details = getattr(err, "error_details", None)
    status = getattr(details, "status", None)
    if status == 6:
        return DegiroError("totp_required", str(err))
    if status == 12:
        return DegiroError("app_approval", str(err))
    if details is None:
        # No structured reply from Degiro: network problem or unparsable response.
        return DegiroError("cannot_connect", str(err))
    return DegiroError("invalid_auth", str(err))


def _connect(
    username: str, password: str, totp_secret: str | None, int_account: int | None
) -> API:
    """Log in (blocking) and make sure the account number is known."""
    credentials = Credentials(
        username=username,
        password=password,
        totp_secret_key=totp_secret or None,
        int_account=int_account or None,
    )
    api = API(credentials=credentials)
    try:
        api.connect()
    except DeGiroConnectionError as err:
        raise _map_connect_error(err) from err
    except requests.RequestException as err:
        raise DegiroError("cannot_connect", str(err)) from err

    # connect() only logs in; the update endpoint needs the account number.
    if not credentials.int_account:
        details = api.get_client_details()
        found = ((details or {}).get("data") or {}).get("intAccount")
        if not found:
            raise DegiroError("cannot_connect", "Could not determine the account number")
        credentials.int_account = int(found)
    return api


def _logout(api: API) -> None:
    try:
        api.logout()
    except Exception:  # noqa: BLE001 - best effort, never fail an update over this
        _LOGGER.debug("Degiro logout failed", exc_info=True)


def validate_login(
    username: str, password: str, totp_secret: str | None, int_account: int | None
) -> int:
    """Log in once to verify the credentials; return the account number."""
    api = _connect(username, password, totp_secret, int_account)
    try:
        return int(api.credentials.int_account)
    finally:
        _logout(api)


def _row_to_dict(row: dict) -> dict:
    """Degiro returns rows as [{'name': 'size', 'value': 3}, ...]; flatten them."""
    return {item["name"]: item.get("value") for item in row.get("value", [])}


def fetch_holdings(
    username: str, password: str, totp_secret: str | None, int_account: int | None
) -> dict[str, Any]:
    """Fetch open positions, product info and cash from Degiro (blocking).

    Read-only: only portfolio and product-info requests are made.
    """
    api = _connect(username, password, totp_secret, int_account)
    try:
        update = api.get_update(
            request_list=[
                UpdateRequest(option=UpdateOption.PORTFOLIO, last_updated=0),
                UpdateRequest(option=UpdateOption.TOTAL_PORTFOLIO, last_updated=0),
            ],
            raw=True,
        )
        if not update:
            raise DegiroError("cannot_connect", "Degiro returned no portfolio data")

        rows = [_row_to_dict(r) for r in update.get("portfolio", {}).get("value", [])]
        positions = [
            r
            for r in rows
            if r.get("positionType") == "PRODUCT" and (r.get("size") or 0) > 0
        ]

        totals = _row_to_dict(update.get("totalPortfolio", {}))
        cash = totals.get("totalCash")
        if cash is None:  # fall back to summing the cash rows
            cash = sum(
                (r.get("value") or 0) for r in rows if r.get("positionType") == "CASH"
            )

        product_ids = [int(p["id"]) for p in positions]
        products: dict[str, dict] = {}
        if product_ids:
            info = api.get_products_info(product_list=product_ids, raw=True)
            if not info:
                raise DegiroError("cannot_connect", "Degiro returned no product info")
            products = {str(k): v for k, v in info.get("data", {}).items()}

        out_positions = []
        for pos in positions:
            prod = products.get(str(int(pos["id"])), {})
            out_positions.append(
                {
                    "product_id": int(pos["id"]),
                    "name": prod.get("name"),
                    "isin": prod.get("isin"),
                    "symbol": prod.get("symbol"),
                    "currency": prod.get("currency"),
                    "qty": pos.get("size"),
                    "break_even_price": pos.get("breakEvenPrice"),
                    "degiro_price": pos.get("price"),
                    "degiro_value": pos.get("value"),
                }
            )
        out_positions.sort(key=lambda p: (p["name"] or "").lower())
        return {"cash": float(cash or 0), "positions": out_positions}
    except DegiroError:
        raise
    except DeGiroConnectionError as err:
        raise _map_connect_error(err) from err
    except requests.RequestException as err:
        raise DegiroError("cannot_connect", str(err)) from err
    finally:
        _logout(api)


# ------------------------------------------------------------------- Yahoo --

# Yahoo answers HTTP 429 to plain HTTP clients (aiohttp/requests) on every request.
# yfinance talks to Yahoo through curl_cffi, which presents a browser-like TLS
# fingerprint, so that is what we use. Its own error logging duplicates ours.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

PACE_SECONDS = 0.3  # minimum spacing between Yahoo requests


class YahooError(Exception):
    """A Yahoo Finance request failed or returned something unusable."""


class YahooRateLimited(YahooError):  # noqa: N818
    """Yahoo is throttling us; stop sending requests for a while."""


@dataclass(frozen=True)
class Quote:
    """Last price in major currency units (pence already converted)."""

    price: float
    currency: str
    as_of: str


def _is_rate_limit(err: BaseException) -> bool:
    text = str(err).lower()
    return (
        isinstance(err, YFRateLimitError)
        or "too many requests" in text
        or "rate limit" in text
        or "429" in text
    )


def _quote_sync(symbol: str) -> Quote:
    """Blocking: latest close for a Yahoo symbol, in major currency units."""
    try:
        ticker = yf.Ticker(symbol)
        history = ticker.history(period="5d", auto_adjust=False)
        closes = history["Close"].dropna()
        if closes.empty:
            raise YahooError(f"{symbol}: no price data")
        currency = (ticker.history_metadata or {}).get("currency")
        price = float(closes.iloc[-1])
        as_of = closes.index[-1].date().isoformat()
    except YahooError:
        raise
    except Exception as err:  # noqa: BLE001 - yfinance raises many unrelated types
        if _is_rate_limit(err):
            raise YahooRateLimited(f"{symbol}: Yahoo rate limit") from err
        raise YahooError(f"{symbol}: {err}") from err
    if not currency:
        raise YahooError(f"{symbol}: no currency in Yahoo response")
    major, divisor = normalize_currency(currency)
    return Quote(price / divisor, str(major), as_of)


def _search_sync(query: str) -> list[dict]:
    """Blocking: Yahoo symbol search (works with ISINs)."""
    try:
        return list(yf.Search(query, max_results=10, news_count=0, lists_count=0).quotes)
    except Exception as err:  # noqa: BLE001
        if _is_rate_limit(err):
            raise YahooRateLimited(f"search {query}: Yahoo rate limit") from err
        raise YahooError(f"search {query}: {err}") from err


class YahooClient:
    """Async wrapper around yfinance with request pacing."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._lock = asyncio.Lock()
        self._last = 0.0
        # Set by the coordinator when Yahoo rate-limits us. Lives here (not on the
        # coordinator) because a failed setup is retried with a brand-new coordinator.
        self.cooldown_until: datetime | None = None

    async def _pace(self) -> None:
        async with self._lock:
            wait = self._last + PACE_SECONDS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def quote(self, symbol: str) -> Quote:
        """Return the latest price for a Yahoo symbol."""
        await self._pace()
        return await self._hass.async_add_executor_job(_quote_sync, symbol)

    async def search(self, query: str) -> list[dict]:
        """Search Yahoo; returns the raw quote dicts."""
        await self._pace()
        return await self._hass.async_add_executor_job(_search_sync, query)
