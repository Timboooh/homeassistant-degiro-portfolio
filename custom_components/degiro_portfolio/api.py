"""Clients for Degiro (unofficial, blocking) and Yahoo Finance (async)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote as url_quote

import aiohttp
import requests
from degiro_connector.core.exceptions import (
    CaptchaRequiredError,
    DeGiroConnectionError,
    MaintenanceError,
)
from degiro_connector.trading.api import API
from degiro_connector.trading.models.account import UpdateOption, UpdateRequest
from degiro_connector.trading.models.credentials import Credentials

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

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class YahooError(Exception):
    """A Yahoo Finance request failed or returned something unusable."""


@dataclass(frozen=True)
class Quote:
    """Last price in major currency units (pence already converted)."""

    price: float
    currency: str
    as_of: str


class YahooClient:
    """Minimal async client for Yahoo Finance's public chart/search endpoints."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _get_json(self, url: str, params: dict[str, Any]) -> dict:
        try:
            async with self._session.get(
                url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status == 429:
                    raise YahooError("Yahoo rate limit (HTTP 429)")
                if resp.status != 200:
                    raise YahooError(f"Yahoo returned HTTP {resp.status}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise YahooError(f"Yahoo request failed: {err}") from err

    async def quote(self, symbol: str) -> Quote:
        """Return the latest price for a Yahoo symbol."""
        data = await self._get_json(
            CHART_URL.format(symbol=url_quote(symbol, safe="")),
            {"range": "5d", "interval": "1d"},
        )
        chart = data.get("chart") or {}
        if chart.get("error"):
            raise YahooError(f"{symbol}: {chart['error'].get('description', 'error')}")
        results = chart.get("result") or []
        if not results:
            raise YahooError(f"{symbol}: no data")

        result = results[0]
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            closes = (
                ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close")
                or []
            )
            closes = [c for c in closes if c is not None]
            price = closes[-1] if closes else None
        currency = meta.get("currency")
        if price is None or not currency:
            raise YahooError(f"{symbol}: no price or currency")

        major, divisor = normalize_currency(currency)
        ts = meta.get("regularMarketTime")
        as_of = datetime.fromtimestamp(ts, UTC).date().isoformat() if ts else ""
        return Quote(float(price) / divisor, str(major), as_of)

    async def search(self, query: str) -> list[dict]:
        """Search Yahoo (works with ISINs); returns the raw quote dicts."""
        data = await self._get_json(
            SEARCH_URL,
            {"q": query, "quotesCount": 10, "newsCount": 0, "listsCount": 0},
        )
        return data.get("quotes") or []
