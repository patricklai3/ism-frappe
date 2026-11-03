# Dispatch: M1 Reviewer 4 (Adversarial Code Review & Security)

## Mission
Perform adversarial code review on Milestone 1 code and remediation changes, focusing on Frappe security, RBAC permissions, lifecycle hooks, SQL injection risks, and error handling.

## Mandatory References
1. `d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md` (MUST read first)
2. `d:\ism\PROJECT.md`
3. `d:\ism\.agents\skills\frappe-code-review\SKILL.md`
4. Worker Handoff: `d:\ism\.agents\teamwork\m1_worker_2\handoff.md`

## Focus Areas
- Adversarial inspection of `Schedule Entry` and `Service Bay` permissions and validations.
- Verify `ISM Manager` role permissions in JSON definitions and check for privilege escalation or unauthorized access.
- Verify `ScheduleEntry.validate_estimated_duration()` cannot be bypassed by string representations, floats, or None.
- Verify `ScheduleEntryType.on_trash()` cannot be circumvented to delete standard seeded types.
- Verify test suites pass in WSL and check for transaction isolation leaks.
- Output verdict: **APPROVE** or **REQUEST_CHANGES** in `d:\ism\.agents\teamwork\m1_reviewer_4\handoff.md` and message orchestrator.


## 2026-10-26T16:09:22Z
You are m1_reviewer_4 (M1 Security & Adversarial Reviewer 4). Your working directory is d:\ism\.agents\teamwork\m1_reviewer_4.
Read your dispatch instructions at d:\ism\.agents\teamwork\m1_reviewer_4\DISPATCH.md and the authoritative requirements at d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md.
Load domain skill d:\ism\.agents\skills\frappe-code-review\SKILL.md.
Perform adversarial and security review of Milestone 1 data models, permissions (ISM Manager role), validations, and deletion guards.
Run the 4 DocType test suites in WSL Docker devcontainer-frappe-1.
Deliver your review report with explicit verdict APPROVE or REQUEST_CHANGES in d:\ism\.agents\teamwork\m1_reviewer_4\handoff.md and send_message to orchestrator.
