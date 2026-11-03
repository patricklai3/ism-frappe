# Local copy of frappe-app-dev skill
Source: d:\ism\.agents\skills\frappe-app-dev\SKILL.md

Key rules:
- Always pass `--site <site>` explicitly to bench commands. Never run bare `bench migrate`.
- Run commands in container: `devcontainer-frappe-1`.
- Testing: `bench --site <site> run-tests --app ism`.
