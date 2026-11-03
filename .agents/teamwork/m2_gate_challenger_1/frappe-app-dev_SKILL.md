---
name: frappe-app-dev
description: >-
  Builds full-stack Frappe Framework applications end-to-end. Use this skill any
  time the user mentions: creating or modifying a DocType, writing a controller
  or lifecycle hook, adding a whitelisted API, setting up a new Frappe app or
  bench site, building a desk form or list view, creating portal pages, writing
  background jobs or scheduled tasks, managing permissions or roles, writing
  Frappe tests, or working with frappe.db / frappe.qb. Also applies when the
  user says things like "how do I hook into save", "add a field to a DocType",
  "create a REST endpoint in Frappe", "run bench migrate", or "install an app on
  a site" — even if they don't explicitly say "Frappe".
---

# Frappe Full-Stack App Builder

## Global Rules

- Use bare `bench`. Not `./env/bin/bench`. Not a full path.
- Do not run `which bench`, `bench --version`, `bench --help`, or check frappe version. No discovery commands.
- Do not delegate bench detection to a subagent. Run `ls apps/ sites/ Procfile` yourself.
- Do not create DocType folders with `mkdir`. Frappe creates them via `bench migrate`.
- Run `bench start` in a background process only.
- Before running `bench start`, check if it's already running in an existing terminal. Do not start a second instance.
- Always pass `--site <site>` explicitly to bench commands. Never run bare `bench migrate`.
