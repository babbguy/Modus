# Export a Finance Report

> Pull spend data into your accounting system or a spreadsheet.
> **Audience:** FinOps, finance team. **Time:** 3 minutes.

## What you'll accomplish

- Export burn rate, chargeback, or top-apps data to CSV or JSON
- Import it into Excel / Google Sheets / your GL system

## Steps

### Method 1: CSV / JSON export from any tile

Some data tables in Modus have **CSV** and **JSON** buttons in the tile
header. Click them to download the visible rows.

Tiles with export support:
- **Overview** → Top Apps by Cost, Top Models
- **Policies** → Governance Policies
- **DevOps** → Cost by Deployment, Agent Registry
- **Pricing** → Pricing Overrides, Global Pricing
- **Finance** → Budget Burn Rate by Team
- **Governance** → an Export menu
- **Executive** → a chargeback CSV download

The exported file uses the tile ID and date as the filename, e.g.
`modus-fin-burn-rate-2026-10-07.csv`.

### Method 2: Filter, then export

The export captures only **visible** rows. On tiles that have a filter box
(for example Governance Policies), filter first, then click CSV or JSON; the
download contains only the matching rows.

### Method 3: Generate a chargeback report

For a monthly chargeback:

1. Open **Finance**
2. Click **Generate** on the Chargeback tile and confirm
3. Wait for the table to populate
4. For a file, use the chargeback CSV download in the **Executive** view (the
   Finance Chargeback tile itself has no export buttons), or call
   `GET /api/v1/finance/chargeback`

### Method 4: Scheduled reports

For recurring exports:

1. Create a report with `POST /api/v1/finance/reports`: a `name`, a
   `report_type` (`chargeback`, `burn_rate`, `variance`, `audit_trail` or
   `forecast`), a `schedule` (`daily`, `weekly`, `monthly` or `quarterly`), a
   `delivery_channel` (`webhook` or `slack`), a `delivery_target` (the URL) and
   a `format` (`json` or `csv`).
2. **Finance → Scheduled Reports** lists the reports with their last run and
   status. (Email delivery is not available for scheduled reports.)

## CSV format details

- UTF-8 encoded
- Fields containing commas, quotes or newlines are quoted
- Header row matches the table column titles
- Cell values are the text shown in the table, so currency and number
  formatting (for example `$1,234.56`) is included

## Verify it worked

- Open the downloaded file in Excel — column headers should match the tile

## Troubleshooting

**Nothing downloads.**
The tile has no visible rows (the export does nothing in that case). Clear the
filter or wait for data to load.

**Excel is mangling the dates.**
Excel auto-converts ISO dates. Open the CSV via "Get Data → From Text"
and set the date column type to Text or Date explicitly.

## Related

- [USER_GUIDE.md → Finance](../USER_GUIDE.md#33-finance)
- [Set Up Cost Center Chargeback](04-set-up-chargeback.md)
