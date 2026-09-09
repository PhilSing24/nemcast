# NEMCast — who does what

Four roles, three handoffs. The one rule that makes it a control rather than a process:
**the person who builds a model cannot be the person who approves it.**

---

## The four roles

| Role | Owns | On NEMCast | Cannot |
|---|---|---|---|
| **Data engineer** | Getting the data in and keeping it immutable | Pulled 1.9M dispatch rows and 17M forecast vintages from NEMWEB. Caught AEMO's Aug 2024 filename change that silently broke the standard library. Verified: zero duplicates, all prices `FIRM`, no gaps. | Decide what makes a good feature |
| **Data scientist** | Raw data to candidate model | Defined the target, audited leakage, detected the regime shift before modelling, set the embargo, trained and evaluated. | Approve their own model |
| **Model reviewer** | Independent challenge | Reads the evidence and asks whether the method holds. | Build or modify the model |
| **Approver** | Accountability for deployment | Signs off, or doesn't. | Have built or reviewed it |

---

## The three handoffs

**Data engineer → Data scientist**

Complete, versioned, immutable data. Row counts, date coverage and types verified.
*Gate: is the data trustworthy?*

**Data scientist → Reviewer**

Model trained, test set scored once, evidence attached.
*Gate: is the method sound?*

**Reviewer → Approver**

Documented opinion: pass, fail, or send back.
*Gate: does this go live?*

---

## What the reviewer actually checks

Not the score. The method.

- Was the split chronological, and why does training stop on 24 December?
- Was leakage tested? What was found?
- Is the benchmark computed on the same period as the model?
- Was the test set touched more than once?

All four are answerable from one artefact: `03_pre_modelling_checks.ipynb`. That notebook
is what turns "trust me" into "here's the evidence."

---

## Why the separation matters

**Builder and approver.** Without the split, the workflow documents work rather than
challenging it. It is the first thing a regulated buyer checks, and the reason model risk
functions report independently of the teams whose models they review.

**Data engineer and data scientist.** This one is subtler and matters just as much. If the
person building the model also owns the raw layer, every result becomes unverifiable —
drop the awkward days, adjust a value, re-pull with a different range, and nobody can tell.

It rarely takes dishonesty. "This month looks odd, let me exclude it," repeated a dozen
times in good faith, fits the data to the model just as effectively.

The rule is not that the data scientist cannot exclude data — they must be able to. The
2022 market suspension is not a market outcome. The embargo week is deliberate. What the
separation buys is that **every exclusion is a documented decision against an immutable
baseline, rather than an untraceable absence.**

This pipeline shows the pattern: `nemcast_upload` v1 → filters → three sets. The embargo
appears as a gap you can point at and question, not as rows that were quietly never there.

For the demo, the builder/approver split needs two accounts, not one person switching hats.
The handoff has to be visible in the record.
