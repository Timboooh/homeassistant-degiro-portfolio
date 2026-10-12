"""Data coordinator: holdings from Degiro (cached), prices from Yahoo."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AUTH_REASONS,
    DegiroError,
    YahooClient,
    YahooError,
    fetch_holdings,
    normalize_currency,
)
from .const import (
    BASE_CURRENCY,
    CONF_INT_ACCOUNT,
    CONF_TICKER_OVERRIDES,
    CONF_TOTP_SECRET,
    CONF_UPDATE_HOURS,
    DEFAULT_UPDATE_HOURS,
    DEGIRO_RETRY_BACKOFF,
    DEVIATION_WARN,
    DOMAIN,
    HOLDINGS_MAX_AGE,
    RETRY_AFTER_SECONDS,
    STORE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
MAX_SEARCH_CANDIDATES = 6


def parse_ticker_overrides(text: str) -> dict[str, str]:
    """Parse lines like 'IE00BK5BQT80=VWCE.DE' into {isin: symbol}.

    Blank lines and lines starting with '#' are ignored.
    Raises ValueError on a malformed line.
    """
    overrides: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        isin, sep, symbol = line.partition("=")
        isin, symbol = isin.strip().upper(), symbol.strip()
        if not sep or not symbol or not ISIN_RE.match(isin):
            raise ValueError(f"Invalid line: {raw!r}")
        overrides[isin] = symbol
    return overrides


@dataclass
class PortfolioData:
    """Result of one update."""

    total: float
    investments: float
    cash: float
    positions: list[dict[str, Any]]
    updated_at: datetime
    holdings_updated_at: datetime | None
    fallback_positions: list[str] = field(default_factory=list)


class _Fx:
    """Per-update cache of conversion factors to EUR."""

    def __init__(self, yahoo: YahooClient) -> None:
        self._yahoo = yahoo
        self._cache: dict[str, float] = {BASE_CURRENCY: 1.0}

    async def to_eur(self, currency: str) -> float:
        if currency not in self._cache:
            quote = await self._yahoo.quote(f"{BASE_CURRENCY}{currency}=X")
            if quote.price <= 0:
                raise YahooError(f"Invalid FX rate for {currency}")
            self._cache[currency] = 1.0 / quote.price
        return self._cache[currency]


class DegiroPortfolioCoordinator(DataUpdateCoordinator[PortfolioData]):
    """Fetches holdings (rarely) and prices (every interval)."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        hours = int(entry.options.get(CONF_UPDATE_HOURS, DEFAULT_UPDATE_HOURS))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(hours=hours),
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._stored: dict[str, Any] = {}
        self._yahoo = YahooClient(async_get_clientsession(hass))
        self._force_holdings = False
        self._auth_failed = False
        self._degiro_retry_at: datetime | None = None

    async def _async_setup(self) -> None:
        self._stored = await self._store.async_load() or {}

    async def async_refresh_holdings(self) -> None:
        """Re-read the portfolio from Degiro on the next refresh (button)."""
        self._force_holdings = True
        self._degiro_retry_at = None
        await self.async_request_refresh()

    # ------------------------------------------------------------ holdings --

    async def _async_get_holdings(
        self, now: datetime
    ) -> tuple[dict[str, Any], datetime | None]:
        holdings = self._stored.get("holdings")
        fetched_at = dt_util.parse_datetime(self._stored.get("holdings_updated_at") or "")

        due = (
            self._force_holdings
            or holdings is None
            or fetched_at is None
            or now - fetched_at >= HOLDINGS_MAX_AGE
        )
        if not due:
            return holdings, fetched_at

        if self._auth_failed:
            # Don't hammer Degiro with a password that is known not to work.
            if holdings is None:
                raise ConfigEntryAuthFailed("Degiro credentials are no longer valid")
            return holdings, fetched_at
        if (
            self._degiro_retry_at is not None
            and now < self._degiro_retry_at
            and holdings is not None
        ):
            return holdings, fetched_at

        self._force_holdings = False
        data = self.config_entry.data
        try:
            fresh = await self.hass.async_add_executor_job(
                fetch_holdings,
                data[CONF_USERNAME],
                data[CONF_PASSWORD],
                data.get(CONF_TOTP_SECRET),
                data.get(CONF_INT_ACCOUNT),
            )
        except Exception as err:  # noqa: BLE001
            degiro_err = (
                err
                if isinstance(err, DegiroError)
                else DegiroError("unknown", repr(err))
            )
            if degiro_err.reason == "unknown":
                _LOGGER.exception("Unexpected error while reading Degiro")
            self._degiro_retry_at = now + DEGIRO_RETRY_BACKOFF

            if degiro_err.reason in AUTH_REASONS:
                self._auth_failed = True
                if holdings is None:
                    raise ConfigEntryAuthFailed(str(degiro_err)) from err
                _LOGGER.warning(
                    "Degiro rejected the stored credentials (%s); using holdings "
                    "from %s. Re-authenticate the integration to resume updates",
                    degiro_err,
                    fetched_at,
                )
                self.config_entry.async_start_reauth(self.hass)
                return holdings, fetched_at

            if holdings is None:
                raise UpdateFailed(
                    f"Could not read Degiro ({degiro_err.reason}): {degiro_err}",
                    retry_after=RETRY_AFTER_SECONDS,
                ) from err
            _LOGGER.warning(
                "Degiro update failed (%s: %s); using holdings from %s",
                degiro_err.reason,
                degiro_err,
                fetched_at,
            )
            return holdings, fetched_at

        self._degiro_retry_at = None
        self._stored["holdings"] = fresh
        self._stored["holdings_updated_at"] = now.isoformat()
        await self._store.async_save(self._stored)
        return fresh, now

    # -------------------------------------------------------------- prices --

    async def _search_isin(self, pos: dict[str, Any]) -> str | None:
        """Find the best Yahoo ticker for a position via its ISIN."""
        want_ccy, _ = normalize_currency(pos.get("currency"))
        degiro_symbol = (pos.get("symbol") or "").upper()

        best: str | None = None
        best_score = -1
        tried = 0
        for candidate in await self._yahoo.search(pos["isin"]):
            symbol = candidate.get("symbol")
            if not symbol or candidate.get("quoteType") not in (
                "ETF",
                "EQUITY",
                "MUTUALFUND",
            ):
                continue
            if tried >= MAX_SEARCH_CANDIDATES:
                break
            tried += 1
            try:
                quote = await self._yahoo.quote(symbol)
            except YahooError:
                continue
            score = 2 * (quote.currency == want_ccy) + (
                symbol.split(".")[0].upper() == degiro_symbol
            )
            if score > best_score:
                best, best_score = symbol, score
            if score >= 3:
                break

        if best and best_score < 2:
            _LOGGER.warning(
                "%s: no Yahoo listing trades in %s; using %s anyway",
                pos.get("name"),
                want_ccy,
                best,
            )
        return best

    async def _resolve_ticker(self, pos: dict[str, Any], overrides: dict[str, str]) -> str:
        isin = pos.get("isin")
        if not isin:
            raise YahooError("position has no ISIN")
        if isin in overrides:
            return overrides[isin]
        tickers: dict[str, str] = self._stored.setdefault("tickers", {})
        if isin in tickers:
            return tickers[isin]
        symbol = await self._search_isin(pos)
        if not symbol:
            raise YahooError(f"no Yahoo ticker found for {isin}")
        tickers[isin] = symbol
        _LOGGER.info(
            "Resolved %s (%s) to Yahoo ticker %s. If that is the wrong listing, "
            "set an override in the integration options",
            pos.get("name"),
            isin,
            symbol,
        )
        return symbol

    async def _value_position(
        self, pos: dict[str, Any], overrides: dict[str, str], fx: _Fx
    ) -> dict[str, Any]:
        qty = pos.get("qty") or 0
        degiro_value = pos.get("degiro_value")
        out: dict[str, Any] = {
            "name": pos.get("name"),
            "isin": pos.get("isin"),
            "qty": qty,
            "ticker": None,
            "price": None,
            "price_currency": None,
            "price_date": None,
            "value_eur": 0.0,
            "source": "degiro_fallback",
        }
        try:
            symbol = await self._resolve_ticker(pos, overrides)
            quote = await self._yahoo.quote(symbol)
            value = qty * quote.price * await fx.to_eur(quote.currency)
        except YahooError as err:
            _LOGGER.warning("%s: price lookup failed (%s)", pos.get("name"), err)
            if degiro_value is not None:
                out["value_eur"] = round(float(degiro_value), 2)
            return out

        out.update(
            ticker=symbol,
            price=round(quote.price, 4),
            price_currency=quote.currency,
            price_date=quote.as_of,
            value_eur=round(value, 2),
            source="yahoo",
        )
        if degiro_value:
            deviation = value / degiro_value - 1
            if abs(deviation) > DEVIATION_WARN:
                _LOGGER.warning(
                    "%s: Yahoo value %.2f differs %.1f%% from Degiro's %.2f "
                    "(wrong listing, or stale holdings?)",
                    pos.get("name"),
                    value,
                    deviation * 100,
                    degiro_value,
                )
        return out

    # -------------------------------------------------------------- update --

    async def _async_update_data(self) -> PortfolioData:
        now = dt_util.utcnow()
        holdings, holdings_at = await self._async_get_holdings(now)

        try:
            overrides = parse_ticker_overrides(
                self.config_entry.options.get(CONF_TICKER_OVERRIDES, "")
            )
        except ValueError:
            _LOGGER.warning("Ignoring invalid ticker overrides in the options")
            overrides = {}

        tickers_before = dict(self._stored.get("tickers", {}))
        fx = _Fx(self._yahoo)
        positions = [
            await self._value_position(pos, overrides, fx)
            for pos in holdings.get("positions", [])
        ]

        if self._stored.get("tickers", {}) != tickers_before:
            await self._store.async_save(self._stored)

        if positions and not any(p["source"] == "yahoo" for p in positions):
            # Home Assistant only notifies entities on the first failure in a row;
            # push a state write ourselves so the `stale` attribute keeps ageing.
            self.async_update_listeners()
            raise UpdateFailed(
                "Could not get a price from Yahoo Finance for any position",
                retry_after=RETRY_AFTER_SECONDS,
            )

        investments = round(sum(p["value_eur"] for p in positions), 2)
        cash = round(float(holdings.get("cash") or 0), 2)
        return PortfolioData(
            total=round(investments + cash, 2),
            investments=investments,
            cash=cash,
            positions=positions,
            updated_at=now,
            holdings_updated_at=holdings_at,
            fallback_positions=[
                p["name"] for p in positions if p["source"] != "yahoo"
            ],
        )
