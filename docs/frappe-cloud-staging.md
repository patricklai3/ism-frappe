# Frappe Cloud Multi-Environment Staging & Production Runbook

This runbook deploys `ism` onto **Frappe / ERPNext v16** on Frappe Cloud while isolating live accounting records from staging experiments.

## Architecture Overview

- **Monorepo (`ism`)**: Primary development repo at `d:\ism` containing `ism-frappe/` and `ism-agent-os/`.
- **Deploy repo (`ism-frappe`)**: Dedicated private GitHub repo synced from `ism-frappe/` via `git subtree split`, exposing `pyproject.toml` and `ism/` directly at the repository root as Frappe Cloud requires.
- **Two Separate Private Bench Groups on Frappe Cloud**:
  - `ism-staging`: Tracks `ism-frappe` branch `staging`. Hosts a clone of your live site for app installation, migrations, and testing.
  - `ism-production`: Tracks `ism-frappe` branch `production`. Receives only tested, fast-forward releases.

> Why separate Private Bench Groups? On Frappe Cloud, every site inside one Bench Group shares the exact same app codebase and Git branches, and custom GitHub apps require a Private Bench. Separate Bench Groups guarantee staging deploys never touch live production code or migrations.

---

## 1. GitHub Two-Repo & Subtree Sync Setup

1. Create two private GitHub repositories:
   - `<owner>/ism` (monorepo pushed from `d:\ism`)
   - `<owner>/ism-frappe` (deploy repo consumed by Frappe Cloud; initialize empty, no README/license commit needed)
2. Generate an SSH deploy key for the deploy repo:
   ```bash
   ssh-keygen -t ed25519 -C "ism-frappe-deploy" -f ism_frappe_deploy_key -N ""
   ```
   - On `<owner>/ism-frappe` -> **Settings > Deploy keys**: add `ism_frappe_deploy_key.pub` with **Allow write access** enabled.
   - On `<owner>/ism` -> **Settings > Secrets and variables > Actions**:
     - Add **Variable** `ISM_FRAPPE_DEPLOY_REPO` = `<owner>/ism-frappe`
     - Add **Secret** `ISM_FRAPPE_DEPLOY_KEY` = contents of private key `ism_frappe_deploy_key`
3. Create `staging` and `production` branches in the monorepo (and push):
   - Pushing `staging`, `production`, or `main` in `<owner>/ism` triggers [sync-ism-frappe.yml](../../.github/workflows/sync-ism-frappe.yml) automatically whenever `ism-frappe/**` changes.
4. Optional local push from Windows (`d:\ism`):
   ```powershell
   git remote add frappe-deploy git@github.com:<owner>/ism-frappe.git
   powershell -ExecutionPolicy Bypass -File ism-frappe\scripts\push-deploy-subtree.ps1 -DryRun
   powershell -ExecutionPolicy Bypass -File ism-frappe\scripts\push-deploy-subtree.ps1 -Branch staging
   ```

---

## 2. Create the Staging Private Bench Group (`ism-staging`)

1. In **Frappe Cloud Dashboard > Benches > New Bench Group**, create `ism-staging` on **Frappe Version 16** (Python 3.14).
2. Add apps to `ism-staging`:
   - `frappe` (`version-16`)
   - `erpnext` (`version-16`)
   - `hrms` (`version-16`, optional — only if your live site uses HR/Leave Applications)
   - Custom app `<owner>/ism-frappe` -> select branch **`staging`** (Frappe Cloud verifies `[tool.bench.frappe-dependencies]` in `pyproject.toml` and `required_apps = ["frappe/erpnext"]` in `ism/hooks.py`).
3. Deploy the bench (`ism-staging`) so the code image builds cleanly before cloning your database onto it.

---

## 3. Clone Live Production Safely onto `ism-staging`

Leave your live production site completely untouched during this entire phase.

1. In **Frappe Cloud Dashboard > Sites**, open your live production site -> **Backups** and trigger an **On-Demand Backup** (database + files).
2. Create a new staging site (for example `staging.<yourdomain>.com`) directly inside **`ism-staging`**, or use **Actions > Clone Site** / **Site Migration** to restore the production backup into the new staging site on `ism-staging`.
3. Immediately after the staging site finishes provisioning/restoring, lock down outbound side effects on staging before enabling users or testing:
   - In **Frappe Cloud Site Dashboard > Site Config / Actions**:
     - Enable **Pause Scheduler** (`pause_scheduler: 1`)
     - Disable outgoing email (`disable_emails: 1` or disable Email Account / Email Queue sending)
     - Pause any active Webhooks / payment gateway live credentials
   - Note: `ISM Settings.sms_gateway_provider` defaults to `Disabled` on install; verify it stays `Disabled` (or points only to a sandbox webhook) on staging.
4. Install `ism` onto the **staging site only** via **Site Dashboard > Apps > Install App > `ism`**.
5. Verify live accounting data safety on the staging site:
   - Confirm your Company's existing **Sales Taxes and Charges Template**, **Purchase Taxes and Charges Template**, **Tax Rules**, **Chart of Accounts**, and **Accounts Settings** (`add_taxes_from_taxes_and_charges_template`) remained unchanged. (`ism` only seeds `13% HST - <abbr>` defaults when a company has no active Sales or Purchase tax templates.)
   - Confirm `ISM` workspace / sidebar, `Repair Order`, `Service Package`, `Repair Vehicle`, and `ISM Settings` load cleanly.
   - Verify a test flow on staging (Repair Order -> Service -> Quote Approval -> Sales Invoice / Checkout -> Stock / Purchase Order) against your real Chart of Accounts and Warehouses.
6. Generate API credentials for `ism-agent-os` on staging:
   - Create/select a Desk user with `Service Advisor` / `ISM Manager` permissions, generate API Key + Secret, and test locally from `d:\ism\ism-agent-os` using a staging `.env` (`FRAPPE_URL=https://staging.<yourdomain>.com`, `FRAPPE_API_KEY=...`, `FRAPPE_API_SECRET=...`) via `python -m tools.cli test`.

---

## 4. Production Cutover (`ism-production`)

Only proceed after full sign-off on `ism-staging`.

1. Promote `staging` to `production` in the monorepo via fast-forward merge so exact tested commits ship:
   ```bash
   git checkout production
   git merge --ff-only staging
   git push origin production
   ```
   Wait for `Sync ism-frappe Subtree to Deploy Repo` to update `<owner>/ism-frappe` branch `production`.
2. In **Frappe Cloud Dashboard > Benches**, create a **new** Private Bench Group `ism-production` on **Version 16** with the same apps as your live site (`frappe`, `erpnext`, optional `hrms`) plus `<owner>/ism-frappe` tracking branch **`production`**, and deploy the bench.
3. Schedule a maintenance window, take a fresh **On-Demand Backup** of the live production site, and move the live site onto `ism-production` via **Site Dashboard > Actions > Change Bench**.
4. Install `ism` on the live site (**Site Dashboard > Apps > Install App > `ism`**).
5. Post-install smoke checks on production:
   - Verify existing Company tax templates, Tax Rules, and Chart of Accounts are untouched.
   - Verify `ISM Settings` defaults (labor rate, operating hours, `sms_gateway_provider`), `Parts` Item Group, and `Equipment Tag` / `Schedule Entry Type` records.
   - Update `ism-agent-os/.env` only when ready to point agent tooling at production.
