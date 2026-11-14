### ISM

ISM

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench install-app ism
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/ism
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### Agent OS Integration

This repository includes [ism-agent-os](./ism-agent-os) as a Git submodule. Clone with submodules to load the shop skills, safety rules, CLI, Python client, and Open WebUI tools over direct Frappe REST (`/api/method/*` and `/api/resource/*`):

```bash
git clone --recurse-submodules <URL_OF_THIS_REPO>
```

Configure Frappe API credentials in `ism-agent-os/.env` (from `ism-agent-os/.env.example`) and verify connectivity:

```bash
cd ism-agent-os
python -m tools.cli test
```

Or install in editable mode (`pip install -e ./ism-agent-os --no-build-isolation`) and run `ism-agent-os test`.

### License

mit
