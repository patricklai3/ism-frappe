# Automotive Sales Workflow Specification

This document defines the sales workflow for the `ism` custom Frappe app. The app extends standard ERPNext sales documents (`Quotation`, `Sales Order`, and `Sales Invoice`) for automotive repair shops.

Use this document to specify features, screen augmentations, and automations for each workflow stage.

---

## 1. Workflow Architecture Overview

```
+-----------------------------------------------------------------------------------+
| STAGE 1: INTAKE & ESTIMATE                                                        |
| DocType: Quotation                                                                |
| Focus: Vehicle ID, customer symptoms, canned jobs, parts & labor estimates        |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| HANDOFF 1: ESTIMATE AUTHORIZATION                                                 |
| Transition: Quotation -> Sales Order                                              |
| Focus: Digital authorization, partial approvals, parts reservation, bay dispatch  |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| STAGE 2: REPAIR ORDER EXECUTION                                                   |
| DocType: Sales Order                                                              |
| Focus: Technician clock-in, parts tracking, supplements, quality control          |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| HANDOFF 2: WORK COMPLETION & BILLING AUDIT                                        |
| Transition: Sales Order -> Sales Invoice                                          |
| Focus: Billed vs actual hours, core charges, shop fees, customer readiness        |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| STAGE 3: INVOICING & VEHICLE RELEASE                                              |
| DocType: Sales Invoice                                                            |
| Focus: Payment processing, split tender, warranty printout, vehicle release gate   |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| POST-SERVICE: RETENTION & REMINDERS                                               |
| Focus: Maintenance stickers, deferred service reminders, review requests          |
+-----------------------------------------------------------------------------------+
```

---

## 2. Stage 1: Vehicle Intake & Estimate (`Quotation`)

### Purpose
Record customer concerns, identify the vehicle, inspect condition, and prepare an itemized repair estimate.

### Standard ERPNext Baseline
- `Quotation` tracks customer, items table (flat list of parts/services), taxes, and standard terms.
- Does not have vehicle tracking, job bundling, flat-rate labor rules, or declined service tracking.

### Proposed Data Fields
| Field Name | Type | Purpose | User Fillable / Status |
| :--- | :--- | :--- | :--- |
| `vehicle` | Link (`Vehicle`) | Links estimate to vehicle record (VIN, plate, make, model, year, engine) | `[ ] Planned` |
| `odometer_in` | Int / Float | Records odometer reading at vehicle drop-off | `[ ] Planned` |
| `customer_concern` | Small Text | Primary reason for visit in customer's words | `[ ] Planned` |
| `job_category` | Select / Link | E.g., Maintenance, Diagnostics, Mechanical, Tires | `[ ] Planned` |
| `service_packages` | Table | Groups labor and parts into discrete canned jobs (e.g. Front Brake Job) | `[ ] Planned` |
| `declined_jobs` | Table | Stores jobs declined by customer for future marketing | `[ ] Planned` |
| `parts_markup_matrix` | Link / Rule | Matrix to calculate retail price from wholesale cost | `[ ] Planned` |

### UX & Screen Augmentations
- **Vehicle Quick-Intake Bar**: Header section showing vehicle details, license plate, VIN, and last service date.
- **Job-Centric Item Hierarchy**: Group parts and labor by "Job / Operation" instead of a single flat items table.
- **Parts Matrix Pricing**: Auto-fill retail prices on part selection based on shop cost brackets.
- **Digital Inspection Link**: Quick action button to attach vehicle walkaround notes and photos.

### Automations & Add-ons
- **VIN Decoder Integration**: Auto-populate vehicle year, make, model, and engine from VIN entry.
- **Customer Vehicle History Lookup**: Show past invoices and outstanding recommendations when vehicle is selected.
- **Digital Authorization Dispatch**: Send SMS or email link with interactive job approve/decline toggles.

### Your Notes & Specifications
<!-- Fill in your specific requirements, preferred fields, or business rules below -->
- **Intake Flow Details**:
  - 
- **Canned Job / Service Package Structure**:
  - 
- **Estimate Approval Rules**:
  - 

---

## 3. Handoff 1: Quotation to Sales Order (Estimate -> Repair Order)

### Purpose
Convert approved estimate lines into an active Repair Order (RO), reserve or order required parts, and assign work to technicians.

### Standard ERPNext Baseline
- Quotation creates Sales Order via the standard "Make -> Sales Order" button.
- Copies all items unconditionally from Quotation to Sales Order.

### Required Validations & Transition Logic
1. **Partial Approvals**: Support customer accepting Job A (brakes) while declining Job B (transmission flush).
2. **Signature & Legal Consent**: Check that customer signature or digital SMS confirmation exists before RO creation.
3. **Deferred Items Log**: Automatically move unapproved lines into a "Deferred Service" log on the Vehicle record.

### Automations & Add-ons
- **Material Request Trigger**: Auto-generate Purchase Orders or stock reservations for parts on approved jobs.
- **Bay / Technician Scheduling**: Create schedule blocks or dispatch entries for assigned technicians.
- **Printed Job Card**: Generate a technician worksheet that hides prices and shows symptoms and labor instructions.

### Your Notes & Specifications
<!-- Fill in your specific requirements, preferred fields, or business rules below -->
- **Approval Capture Method (Signature pad, SMS link, verbal confirmation log)**:
  - 
- **Handling of Unapproved / Declined Items**:
  - 
- **Stock Reservation Rules**:
  - 

---

## 4. Stage 2: Active Repair Order Execution (`Sales Order`)

### Purpose
Track hands-on repair work, technician labor hours, parts delivery, and secondary findings.

### Standard ERPNext Baseline
- `Sales Order` tracks item fulfillment and billing status.
- Does not track bay status, clock-in labor, flat-rate hours vs actual hours, or shop teardown supplements.

### Proposed Data Fields
| Field Name | Type | Purpose | User Fillable / Status |
| :--- | :--- | :--- | :--- |
| `repair_status` | Select | E.g., Waiting on Parts, In Progress, Waiting for Approval, Completed | `[ ] Planned` |
| `technician_assignments` | Table | Assigned technicians and flat-rate hour split per job | `[ ] Planned` |
| `labor_time_logs` | Table | Actual clocked time per technician for efficiency and payroll calculations | `[ ] Planned` |
| `parts_status` | Select | E.g., In Stock, Ordered, Delivered, Returned | `[ ] Planned` |
| `supplement_count` | Int | Number of secondary change orders added during teardown | `[ ] Planned` |

### UX & Screen Augmentations
- **Technician Execution View**: Simplified mobile or tablet view for mechanics to clock in, view instructions, and add notes.
- **Live Parts Tracking Badge**: Visual tag indicating if ordered parts have arrived at the parts counter.
- **One-Click Supplement Creator**: Add a secondary repair item without restarting the entire document.

### Automations & Add-ons
- **Customer Progress Updates**: Auto-trigger SMS notifications when status changes (e.g., "Parts arrived, work started").
- **Supplement Authorization**: Generate an add-on quote link when technician uncovers hidden damage.
- **Teardown / Quality Control Checklist**: Mandatory inspection check before repair order can be marked complete.

### Your Notes & Specifications
<!-- Fill in your specific requirements, preferred fields, or business rules below -->
- **Labor Tracking Model (Flat-Rate Book Time vs Clocked Actual Time)**:
  - 
- **Technician Shop View Needs**:
  - 
- **Supplement / Change Order Process**:
  - 

---

## 5. Handoff 2: Sales Order to Sales Invoice (RO -> Billing)

### Purpose
Audit completed labor, account for shop supplies, calculate environmental fees, and verify parts usage before customer payment.

### Standard ERPNext Baseline
- Sales Order creates Sales Invoice via "Make -> Sales Invoice".
- Copies lines directly without auto-calculating automotive fee structures.

### Required Validations & Transition Logic
1. **Work Completion Sign-off**: Verify quality control sign-off is checked.
2. **Odometer Out Check**: Make sure `odometer_out` is recorded and is greater than `odometer_in`.
3. **Core Return Verification**: Flag parts with core deposits that must be returned to vendor or credited to customer.
4. **Final Hour Audit**: Confirm billed labor hours match customer authorization.

### Automations & Add-ons
- **Shop Supplies / EPA Fee Auto-Calculation**: Auto-add percentage fee (e.g. 5% labor, capped at $50) for hazardous disposal and shop materials.
- **Ready for Pickup Notification**: Auto-send text message: "Your vehicle is ready. Total due: $X. View breakdown here."

### Your Notes & Specifications
<!-- Fill in your specific requirements, preferred fields, or business rules below -->
- **Shop Supplies & EPA Fee Rules**:
  - 
- **Core Charge Handling**:
  - 
- **Pre-Invoice Audit Checklist**:
  - 

---

## 6. Stage 3: Invoicing & Vehicle Release (`Sales Invoice`)

### Purpose
Collect payment, finalize financial records, provide warranty terms, and release keys to the customer.

### Standard ERPNext Baseline
- `Sales Invoice` supports standard POS, partial payments, payment terms, and accounting general ledger entries.
- Missing automotive invoice layouts, service interval stickers, and release authorization gates.

### Proposed Data Fields
| Field Name | Type | Purpose | User Fillable / Status |
| :--- | :--- | :--- | :--- |
| `odometer_out` | Int / Float | Recorded mileage when vehicle leaves shop | `[ ] Planned` |
| `next_service_date` | Date | Recommended next visit date | `[ ] Planned` |
| `next_service_mileage`| Int | Recommended next oil / service mileage | `[ ] Planned` |
| `warranty_terms` | Text | E.g., 24 Months / 24,000 Miles Warranty statement | `[ ] Planned` |
| `key_tag_number` | Data | Physical hook / tag number for vehicle keys | `[ ] Planned` |
| `vehicle_released` | Check | Release gate confirmation before customer leaves | `[ ] Planned` |

### UX & Screen Augmentations
- **Automotive Invoice Print Format**: Customer-friendly printout grouping parts, labor, warranties, and technician notes.
- **Split Payment Modal**: Easy split payment interface (e.g., cash, credit card, fleet account, financing).
- **Service Reminder Sticker Generator**: One-click print for windshield oil change reminder stickers.

### Automations & Add-ons
- **Integrated Payment Terminal Hook**: Send balance directly to credit card terminal.
- **Vehicle Release Validation**: Block marking invoice "Released" if balance is unpaid (unless customer has credit account).
- **Digital Receipt Delivery**: Email and SMS itemized receipt link with inspection photos.

### Your Notes & Specifications
<!-- Fill in your specific requirements, preferred fields, or business rules below -->
- **Payment Methods & Fleet Terms**:
  - 
- **Invoice Printout Requirements**:
  - 
- **Warranty Statement & Disclaimers**:
  - 

---

## 7. Post-Service: Retention & Maintenance Lifecycle

### Purpose
Keep the customer returning for scheduled maintenance and resolve post-repair issues.

### Automations & Add-ons
- **Review Request**: Send review request SMS 24-48 hours after invoice settlement.
- **Scheduled Maintenance Reminders**: Automated reminders based on elapsed months or predicted mileage.
- **Declined Service Follow-up**: Automated outreach for deferred safety items quoted earlier.

### Your Notes & Specifications
<!-- Fill in your specific requirements, preferred fields, or business rules below -->
- **Follow-up Timing & Channels (SMS / Email)**:
  - 
- **Maintenance Interval Logic**:
  - 

---

## 8. Auxiliary System Connections (For Future Expansion)

While the sales workflow is the operational spine, it directly connects to these auxiliary systems:

```
                  +--------------------------------+
                  |         PARTS INVENTORY        |
                  | - Stock levels & parts matrix  |
                  | - Special orders from vendors  |
                  | - Core returns tracking        |
                  +---------------+----------------+
                                  |
                                  v
+-----------------------+   Sales Engine   +------------------------+
|      PAYROLL & HR     |<---------------->|      CUSTOMER CRM      |
| - Flat-rate vs actual |                  | - Vehicle fleet log    |
| - Technician dispatch |                  | - SMS/Email approvals  |
| - Labor efficiency    |                  | - Service reminders    |
+-----------------------+                  +------------------------+
                                  ^
                                  |
                  +---------------+----------------+
                  |      ACCOUNTING & BILLING      |
                  | - Fleet receivable accounts    |
                  | - EPA / Shop fee ledgers       |
                  | - Warranty reserve tracking    |
                  +--------------------------------+
```

### 1. Parts Inventory
- Sourcing non-stock parts directly from repair estimates.
- Tracking cores returned to parts vendors for credit.
- Managing fast-moving shop stock (fluids, filters, fasteners).

### 2. Technician Payroll & Efficiency
- Flat-rate billed hours credited to technicians.
- Clocked hours compared against book hours for efficiency reporting.
- Tiered commission / flat-rate pay structures.

### 3. Customer & Vehicle Fleet CRM
- Linking multiple vehicles to commercial fleet accounts.
- Fleet-specific labor rates and discount structures.

### 4. Accounting & Compliance
- Hazmat/EPA fee ledger distribution.
- Core inventory tracking account.

---

## 9. Feature Priority & Dependency Matrix

As features are scoped, classify them here:

| Feature ID | Feature Name | Workflow Stage | Depends On | Priority (`P0` / `P1` / `P2`) | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `FEAT-001` | Vehicle DocType & Link | Quotation | ERPNext Customer | `P0` | Draft |
| `FEAT-002` | Job Package / Canned Jobs | Quotation | ERPNext Item | `P0` | Draft |
| `FEAT-003` | Parts Markup Matrix | Quotation | ERPNext Item Price | `P1` | Draft |
| `FEAT-004` | Digital Customer Approval | Handoff 1 | Quotation | `P1` | Draft |
| `FEAT-005` | Technician Labor Time Logs | Sales Order | ERPNext Employee | `P1` | Draft |
| `FEAT-006` | Shop Supplies & EPA Fees | Sales Invoice | Sales Taxes & Charges | `P0` | Draft |
| `FEAT-007` | Automotive Print Formats | Sales Invoice | Print Format | `P0` | Draft |
| `FEAT-008` | Service Reminder Automation | Post-Service | Sales Invoice | `P2` | Draft |

---

## 10. Next Steps

1. Review each stage above and fill in specific requirements under **Your Notes & Specifications**.
2. Identify which features are essential for Minimum Viable Product (`P0`).
3. Refine dependencies between the sales engine and auxiliary modules (Inventory, Payroll).
