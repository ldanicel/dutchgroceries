# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's
[security advisories](https://github.com/ldanicel/dutchgroceries/security/advisories/new) rather than in a
public issue. You can expect an answer within a week.

## How the app is meant to be run

groceries is a single-user, self-hosted app. It holds long-lived logins to your supermarket accounts
and your full purchase history, so treat it like a password manager:

- **Keep it private.** By default it listens on `127.0.0.1` only. If you expose it to your LAN or
  beyond, turn on `GROCERIES_BASIC_AUTH` or run it behind an authenticating reverse proxy with
  `GROCERIES_AUTH_PROXY_HEADER` (plus `GROCERIES_TRUSTED_PROXIES`, so only
  the proxy can set it). The dashboard can connect, disconnect and sync accounts.
- **Host names.** Requests for host names other than localhost, IP addresses, `GROCERIES_HOST` and
  `GROCERIES_ALLOWED_HOSTS` are refused (DNS rebinding protection). Add the name you use.
- **Behind a TLS proxy**, set `GROCERIES_TRUSTED_PROXIES` to the proxy's address so the app knows the
  page is https (secure cookies, correct origin checks).

## What protects your accounts

- Supermarket passwords never reach the app: you log in on the retailer's own page and only a one-time
  code is handed back.
- Tokens live only in `GROCERIES_SECRETS_DIR` (directory 700, files 600), written atomically. They are
  never sent to the browser or logged; codes travel in URL fragments, which do not reach access logs.
- A web login is bound to the browser that started it (random state, HttpOnly SameSite=Strict cookie,
  15 minutes, single use), uses PKCE where the retailer supports it, and is only completed after you
  confirm it on the dashboard.
- State-changing endpoints accept only same-origin JSON POSTs.
- The retailer APIs are unofficial and undocumented; their behaviour can change at any time.
