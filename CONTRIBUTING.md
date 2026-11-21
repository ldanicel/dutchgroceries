# Contributing

Thanks for helping. Bug reports, fixes for API changes, and new retailers are all welcome.

## Development setup

```sh
git clone https://github.com/ldanicel/dutchgroceries.git && cd dutchgroceries
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

To work on the dashboard without an account:

```sh
GROCERIES_DATA_DIR=./demo .venv/bin/groceries demo-seed
GROCERIES_DATA_DIR=./demo .venv/bin/groceries serve
```

## Develop in a container

The repository has a [dev container](https://containers.dev/) (`.devcontainer/devcontainer.json`): Python
3.13 with `pip install -e '.[dev]'`, ruff and pytest, and the VS Code Python and Ruff extensions. Open the
folder in VS Code and choose *Reopen in Container*, or use the CLI:

```sh
devcontainer up --workspace-folder .
devcontainer exec --workspace-folder . pytest
devcontainer exec --workspace-folder . groceries demo-seed
devcontainer exec --workspace-folder . groceries serve    # forwarded on http://localhost:8080
```

Inside the container `GROCERIES_DATA_DIR` is `./demo` and the dashboard listens on port 8080.

To debug a parser with a real (redacted) receipt, copy it to `demo/raw/<retailer>/<channel>/` and run
`groceries reimport --dry-run`; see *Debugging* in the README.

## Guidelines

- Keep pull requests focused, with tests for behaviour changes. CI runs ruff and pytest on Python 3.11
  and 3.13.
- **Never commit real data.** Receipts, API responses, tokens, screenshots and logs from your own account
  contain your store visits, payment details and loyalty ids. Fixtures must be synthetic: invent the
  stores, ids and products.
- The retailers' APIs are unofficial. Keep request rates low and do not add features that scrape other
  people's data or bypass the retailers' own login.
- Dependencies are pinned in `requirements.lock` (runtime) and `pyproject.toml`. Update both together.

Adding a supermarket? See [docs/adding-a-retailer.md](docs/adding-a-retailer.md). How the pieces fit
together: [docs/architecture.md](docs/architecture.md).

## Reporting security issues

Please do not open a public issue; see [SECURITY.md](SECURITY.md).
