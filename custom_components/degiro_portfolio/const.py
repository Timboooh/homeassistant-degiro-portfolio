"""Constants for the Degiro Portfolio integration."""

from datetime import timedelta

DOMAIN = "degiro_portfolio"

CONF_TOTP_SECRET = "totp_secret_key"
CONF_INT_ACCOUNT = "int_account"
CONF_UPDATE_HOURS = "update_hours"
CONF_TICKER_OVERRIDES = "ticker_overrides"

BASE_CURRENCY = "EUR"

DEFAULT_UPDATE_HOURS = 24
MIN_UPDATE_HOURS = 1
MAX_UPDATE_HOURS = 168

# Degiro is only contacted when the stored holdings are at least this old
# (or when the "Refresh from Degiro" button is pressed). Prices come from Yahoo.
HOLDINGS_MAX_AGE = timedelta(hours=20)
# After a failed Degiro attempt, wait this long before trying again.
DEGIRO_RETRY_BACKOFF = timedelta(hours=6)
# Data older than this is flagged with the `stale` attribute.
STALE_AFTER = timedelta(hours=48)
# When nothing could be priced, retry setup/update after this many seconds.
RETRY_AFTER_SECONDS = 1800

# Warn when the Yahoo value differs from Degiro's own value by more than this.
DEVIATION_WARN = 0.05

STORE_VERSION = 1
