# Degiro Portfolio for Home Assistant

> **This is a 100% vibecoded unofficial integration**. Degiro has no public API, so it can break when
> Degiro changes something, and automated logins may be against their terms.
> Use it at your own risk.

Shows your Degiro portfolio value in Home Assistant.

- **Holdings** come from Degiro's unofficial web API (read-only) through the
  [`degiro-connector`](https://pypi.org/project/degiro-connector/) library.
  Degiro is contacted about **once a day**, and the holdings are cached, so a Home
  Assistant restart does not trigger a login.
- **Prices** come from Yahoo Finance and are converted to EUR.
- If Degiro or Yahoo is unreachable, the last known values are kept and the
  `stale` attribute turns `true` after 48 hours.

## Entities

| Entity | Description |
|---|---|
| `sensor.degiro_portfolio_value` | Total (investments + cash). Attributes: `investments`, `cash`, `positions`, `stale`, `last_updated`, `holdings_updated_at`, `fallback_positions` |
| `sensor.degiro_portfolio_investments` | Investments only |
| `sensor.degiro_portfolio_cash` | Cash only |
| `button.degiro_portfolio_refresh_from_degiro` | Re-read your portfolio from Degiro now (press after buying or selling) |

The sensors use `state_class: total`, so Home Assistant keeps long-term statistics
and the "change over the past day/week/month/year" cards work.

## Install

### HACS custom repository

1. In HACS: three dots, **Custom repositories**, paste the repo URL, category **Integration**.
2. Download it, then restart Home Assistant.

### Set up

Settings, Devices & services, **Add integration**, **Degiro Portfolio**.

- **Username / password**: your Degiro login.
- **2FA secret (TOTP)**: required if two-factor authentication is on. This is the
  base32 secret shown when you enrolled your authenticator app (not a 6-digit code).
  If you did not save it, re-enroll 2FA in Degiro to get a new one. SMS-only 2FA
  cannot be used.

The first start installs the `degiro-connector` package, which needs internet.

## Options

Settings, Devices & services, Degiro Portfolio, **Configure**.

- **Price refresh interval** (default 24 h): how often prices are refreshed. Degiro
  itself is still only contacted about once a day.
- **Yahoo ticker overrides**: on first sight of each holding, the integration looks
  up its ISIN on Yahoo and picks a listing in the same currency as your Degiro
  position. The choice is cached and logged (look for `Resolved ... to Yahoo ticker`).
  If it picked the wrong listing, override it, one line per holding:

  ```
  IE00BK5BQT80=VWCE.DE
  IE00B3XXRP09=VUSA.AS
  ```

A warning appears in the log whenever a position's Yahoo value differs more than 5%
from Degiro's own value. That usually means a wrong listing or an outdated holding.

## Notes on credentials

The username, password and TOTP secret are stored in Home Assistant's configuration
storage (`/config/.storage/core.config_entries`). It is not encrypted on disk and it
ends up in backups, so keep backups private (use backup encryption). Anyone with
access to your Home Assistant files can use them to log in to Degiro.

## Troubleshooting

| Symptom | Meaning |
|---|---|
| "Degiro requires a captcha" | Log in once in a browser, solve the captcha, retry. |
| "Open the Degiro app and approve" | Degiro asks for in-app approval; approve and submit again. |
| Repair/re-auth notification appears | Degiro rejected the stored credentials. Open it and enter the current password. Until then, cached holdings keep being used. |
| `Yahoo rate limit (HTTP 429)` in the log | Yahoo is throttling you. Prices are retried every 30 minutes; increase the refresh interval. |
| Sensors show a value but `stale: true` | Degiro or Yahoo has been failing for over 48 hours. Check the log. |

Debug logging:

```yaml
logger:
  logs:
    custom_components.degiro_portfolio: debug
```
