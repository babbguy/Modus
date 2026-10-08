# Modus Step-by-Step Guides — Index

> The registry of user-facing tutorials. Each guide is a single Markdown file
> in this folder. The dashboard's **Help** view shows a summary card for each
> guide and links to the full text here on GitHub.
>
> **To add a new guide:**
> 1. Drop the new `.md` file in this folder, named `NN-kebab-case-title.md`
>    where `NN` is a two-digit ordering number.
> 2. Add an entry to the table below AND to the `GUIDES` array in
>    `dashboard/js/help.js` so the in-app Help view picks it up.
> 3. Cross-link from `docs/USER_GUIDE.md` if it covers a "Common Tasks" entry.

## Guide Catalog

| # | Guide | Audience | Time |
|---|---|---|---|
| 01 | [Set Your First Budget](01-set-your-first-budget.md) | FinOps, Eng Manager | 5 min |
| 02 | [Block Expensive Models](02-block-expensive-models.md) | DevOps, Eng Manager | 5 min |
| 03 | [Investigate a Cost Spike](03-investigate-cost-spike.md) | DevOps, On-call | 8 min |
| 04 | [Set Up Cost Center Chargeback](04-set-up-chargeback.md) | FinOps, Finance | 12 min |
| 05 | [Route to Cheaper Models](05-route-to-cheaper-models.md) | DevOps, Eng Manager | 10 min |
| 06 | [Handle a Budget Breach Alert](06-handle-budget-breach-alert.md) | On-call, FinOps | 5 min |
| 07 | [Export a Finance Report](07-export-finance-report.md) | FinOps, Finance | 3 min |
| 08 | [Instrument Your First App](08-instrument-first-app.md) | Developer | 10 min |

## Conventions

Every guide follows the same structure so users build muscle memory:

```markdown
# Title

> One-sentence pitch. Audience. Time estimate.

## What you'll accomplish
[bulleted outcomes]

## Before you start
[prerequisites]

## Steps
1. ...
2. ...

## Verify it worked
[how to confirm]

## Troubleshooting
[common gotchas]

## Related
[links to other guides + USER_GUIDE.md sections]
```

## Versioning

Guides are versioned with the codebase. When a feature changes in a way
that breaks an existing guide, update the guide in the same pull request.
