# Frappe App Dev Skill Reference
(Dumped from d:\ism\.agents\skills\frappe-app-dev\SKILL.md)

Key execution rules:
- Run tests via bench: `bench --site development.localhost run-tests --app ism ...`
- Container execution: `wsl docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 ...`
- Testing doctrine: Verify DocTypes, single values, fixtures, database transactions.
