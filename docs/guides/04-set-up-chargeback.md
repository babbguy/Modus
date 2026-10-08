# Set Up Cost Center Chargeback

> Allocate AI spend back to the business units that consumed it. **Audience:**
> FinOps lead, finance team. **Time:** 12 minutes.

## What you'll accomplish

- Create cost centers that mirror your accounting structure
- Assign teams to cost centers
- Generate the first chargeback allocation
- Reconcile against the provider invoice

## Before you start

- You know your accounting cost center codes (e.g. `ENG-001`, `MKT-042`)
- You have at least one team registered with usage history
- You have platform admin or finance role permissions

## Steps

### 1. Open the Finance view

Click **Finance** in the sidebar. Scroll to the bottom — you'll find the
**Chargeback**, **Reconciliation Status** and **Cost Center Assignment**
tiles.

### 2. Create a cost center

Click **+ Assign** on the Cost Center Assignment tile.

Fill in the modal:

- **Cost Center Name**: e.g. `Engineering`
- **Code**: e.g. `ENG-001` (must match your accounting system)
- **Owner Email**: the budget owner's email, stored with the cost center
- **Monthly Budget ($)**: the cost center's monthly AI budget

Click **Create Cost Center**. The new center appears in the table.

### 3. Repeat for every cost center

Create one cost center per business unit. Typical organizations have
3-15 of these.

### 4. Assign teams to cost centers

> **Note:** there is no dashboard form for this yet; assign teams through
> the API.

For each team, call (admin access, see [guide 01](01-set-your-first-budget.md)):

```bash
curl -s -X POST "$MODUS_URL/api/v1/finance/allocation" \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"team_id": "<team id>", "cost_center_id": "<cost center id>", "allocation_pct": 100}'
```

`GET /api/v1/finance/cost-centers` lists the cost center ids and
`GET /api/v1/finance/allocation` shows the current assignments.

### 5. Generate the first chargeback allocation

Back on the **Finance** view, click **Generate** on the Chargeback tile.

A confirmation modal appears. Click **Generate**.

Modus computes the allocation: it sums spend by team, app, provider and model
from the start of the current month until now (the API also accepts a
`period_start`/`period_end`), attributes it to each team's cost center and
stores the result. The Chargeback tile then lists it. Generating requires a
platform admin.

### 6. Verify the totals

The Chargeback table shows: Cost Center | Team | Allocated | Period.

The total at the top should equal your **MTD Spend** KPI in the Finance
KPI row. If it doesn't, you have unassigned teams (their spend is missing
from the chargeback) — go assign them.

### 7. Reconcile against provider invoices

**Reconciliation Status** compares the cost Modus tracked with what your
provider billed. Modus does **not** fetch invoices from providers (the billing
connection records only store credentials); you import the invoice totals:

- In the dashboard: **Finance, Reconciliation Status, Import CSV**.
- Or with the API (platform admin; the master key works):

```bash
curl -s -X POST "$MODUS_URL/api/v1/finance/reconciliation/import" \
  -H "X-Modus-APIKey: $MODUS_MASTER_API_KEY" -H "Content-Type: application/json" \
  -d '{"rows": [{"provider": "openai", "service": "API",
        "period_start": "2026-09-01", "period_end": "2026-10-01",
        "actual_cost_usd": "1234.56"}], "source": "openai-invoice-2026-09"}'

curl -s -X POST "$MODUS_URL/api/v1/finance/reconciliation/import/csv" \
  -H "X-Modus-APIKey: $MODUS_MASTER_API_KEY" -F file=@invoices.csv
```

CSV header: `provider,period_start,period_end,actual_cost_usd` plus optional
`service` and `currency` (USD only; convert other currencies first). Rules:

- `provider` is the name Modus records the usage under (`openai`, `anthropic`,
  `azure`, `gcp`, ...; an `aws` invoice is matched to `bedrock` usage).
- The period is `[period_start, period_end)` in UTC; a date means midnight UTC.
- Amounts are decimal strings with at most 8 decimal places; JSON floats are
  rejected.
- One bad row rejects the whole file (HTTP 422, with row numbers); nothing is
  imported.
- Rows are keyed on provider + service + period, so importing the same file
  again updates instead of duplicating.

The tile shows billed vs tracked per row and in total. **Tracked** is recomputed
from the daily usage aggregates every time you open the view, so it reflects
late-arriving usage; variance = billed - tracked, and a variance within 2% of
the billed amount counts as matched. Also enter negotiated rates as pricing
overrides so tracked costs use your contract prices.

## Verify it worked

1. Compare the Chargeback table with your accounting records. (The Chargeback
   tile has no export button; the Executive view's **Chargeback by Team** tile
   has a CSV download.)
2. If they match within tolerance, you're done.

## Troubleshooting

**The Chargeback table is empty after Generate.**
Most likely no team has a cost center assigned, or there was no usage in the
period. Assign at least one team and try again.

**The Department donut chart is empty.**
Same issue — the donut sources from cost-center-assigned teams only.

**Tracked cost differs from the invoice.**
Check Pricing Overrides. If your negotiated rate isn't entered, Modus
is using list price while the provider bills your contract price.

## Related

- [USER_GUIDE.md → Finance](../USER_GUIDE.md#33-finance)
- [Export a Finance Report](07-export-finance-report.md)
