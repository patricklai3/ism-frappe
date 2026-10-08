# ISM - Tesla Service Manual Importer Chrome Extension

This Chrome Extension enables 1-click import of procedures, correction codes, and Flat Rate Times (FRT) directly from the **Tesla Service Manual** (`https://service.tesla.com/*`) into **ISM Repair Orders**.

## Features
- **Auto Context Sync**: Detects your open Repair Order tab in ISM automatically.
- **One-Click Import**: An on-page floating pill displays procedure title, correction code, and FRT (hours). Clicking "Import to Repair Order" inserts the service directly into your RO.
- **Model & Generation Validation**: Detects vehicle platform and generation date ranges, warning you if a procedure doesn't match the vehicle year on your Repair Order.
- **Live Realtime Update**: Your open Repair Order in ISM reloads immediately with the newly added procedure.

## Installation in Chrome / Edge / Brave

1. Open Chrome and navigate to: `chrome://extensions/`
2. Toggle on **Developer mode** (in the top right corner).
3. Click the **Load unpacked** button (top left).
4. Select this directory: `d:\ism\extension`
5. The extension is now active!

## Workflow

1. Open any **Repair Order** in ISM.
2. In the **Services & Jobs** section, click **Tesla Manual**.
3. ISM opens the official Tesla Service Manual for the vehicle's exact generation and sets active RO context.
4. Navigate to any procedure (e.g. *Basic Vehicle Inspection* or *Brake Caliper Overhaul*).
5. Click **Import to Repair Order** on the floating ISM card.
6. The service is added with FRT labor hours and billing amount calculated instantly!
