# Frappe Full-Stack App Builder (Local Copy)

Source: d:\ism\.agents\skills\frappe-app-dev\SKILL.md

Key rules:
- Always pass `--site <site>` explicitly to bench commands. Never run bare `bench migrate`.
- Run tests via bench: `bench --site development.localhost run-tests --app ism ...`
- Adhere to Frappe ORM and DocType naming conventions and testing patterns.
