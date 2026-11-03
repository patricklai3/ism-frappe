# Dispatch: m4_it1_explorer_1

## Objective
Analyze the existing `Repair Order` DocType schema (`ism/ism/doctype/repair_order/repair_order.json`) and determine all required schema changes for Milestone 4:
1. Adding `tab_appointment` initial tab break before "Vehicle Check-in & Intake".
2. Adding provisional fields: `provisional_customer_name` (Data), `provisional_phone` (Phone/Data), `provisional_email` (Data), `provisional_vehicle_notes` (Small Text / Text), and appointments table / section.
3. Adding early status options: `Draft Appointment`, `Scheduled`, `Checked In`, `No Show` (alongside existing statuses like `Draft`, `Work in Progress`, `Completed`, `Cancelled`, etc.).
4. Evaluating mandatory field rules: relaxing mandatory `customer` and `repair_vehicle` during early stages (`Draft Appointment`, `Scheduled`, `No Show`) via `depends_on: "eval:!in_list(['Draft Appointment', 'Scheduled', 'No Show'], doc.status)"` or equivalent Frappe schema conventions.
5. Cross-checking against `ism/tests/test_ro_scheduling_e2e.py` and `ORIGINAL_REQUEST.md` to ensure exact field names and types match.

## Working Directory
`d:\ism\.agents\teamwork\m4_it1_explorer_1`

## Output
Write detailed exploration findings to `d:\ism\.agents\teamwork\m4_it1_explorer_1\handoff.md`.

## 2026-10-27T05:56:31Z
You are an Explorer subagent (m4_it1_explorer_1).
Working directory: d:\ism\.agents\teamwork\m4_it1_explorer_1
Read d:\ism\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.
Read d:\ism\.agents\teamwork\m4_it1_explorer_1\DISPATCH.md for your assignment.
Scope document: d:\ism\PROJECT.md

Task:
Analyze `ism/ism/doctype/repair_order/repair_order.json` and determine all required schema changes for Milestone 4 (Repair Order Integration & Early Appointment Lifecycle):
1. Adding `tab_appointment` initial tab break before "Vehicle Check-in & Intake".
2. Adding provisional fields: `provisional_customer_name`, `provisional_phone`, `provisional_email`, `provisional_vehicle_notes`.
3. Adding early status options: `Draft Appointment`, `Scheduled`, `Checked In`, `No Show`.
4. Conditional mandatory rules: relaxing mandatory `customer` and `repair_vehicle` when status is in `Draft Appointment`, `Scheduled`, `No Show` via `depends_on: "eval:!in_list(['Draft Appointment', 'Scheduled', 'No Show'], doc.status)"` or Frappe schema standards.
5. Cross-checking against `ism/tests/test_ro_scheduling_e2e.py` to ensure exact field names and types match test assertions.

Write your findings and concrete implementation plan to `d:\ism\.agents\teamwork\m4_it1_explorer_1\handoff.md`. Notify caller via send_message when done.
