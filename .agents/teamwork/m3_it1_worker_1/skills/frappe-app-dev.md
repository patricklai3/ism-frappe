# Frappe Full-Stack App Builder (Local Copy)
Source: d:\ism\.agents\skills\frappe-app-dev\SKILL.md

## Global Rules
- Use bare `bench`. Not `./env/bin/bench`. Not a full path.
- Do not run `which bench`, `bench --version`, `bench --help`, or check frappe version. No discovery commands.
- Do not delegate bench detection to a subagent. Run `ls apps/ sites/ Procfile` yourself.
- Do not create DocType folders with `mkdir`. Frappe creates them via `bench migrate`.
- Run `bench start` in a background process only.
- Before running `bench start`, check if it's already running in an existing terminal. Do not start a second instance.
- Always pass `--site <site>` explicitly to bench commands. Never run bare `bench migrate`.
