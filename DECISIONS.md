# DECISIONS.md — assumptions and how ambiguity was handled

Each entry: what was unclear → what I chose → why. All of these are changeable in the policy JSON or one function.

## Policy as data
1. **Policy = versioned JSON** (`policy/policy_v5_0.json`, table `policies`). Rules are an *ordered* list (first match wins) of `when` conditions → `outcome` (decision, route, supplier mode). Limits, windows, ranking weights and consolidation settings are all in that document. No limit appears in code.
2. **Which version applies to a line**: the approved version with the latest `effective_date` ≤ the *requirement date* (date of the first clubbed line), not the wall-clock date. So reprocessing an old batch is reproducible, and an amendment effective mid-batch applies from that day. A draft never affects decisions until approved.
3. **Amendments**: uploaded text/PDF/DOCX or a chat message is interpreted (LLM if `ANTHROPIC_API_KEY` is set, otherwise built-in rules) into a *draft* with a plain-language diff. A Procurement user approves/rejects/edits (JSON editor as the escape hatch). If the amendment has no effective date, the draft keeps the base date and says so.
4. Rule fallbacks: a rule whose outcome needs a contract/preferred supplier that does not exist is skipped (next rule is tried) and the reason is recorded.

## Reading the data
5. **Urgency**: the `urgency` column is authoritative. The words "urgent"/"repeat"/"as per sample" in descriptions are treated as noise for item matching and do *not* make a line Emergency (policy says "marked Emergency").
6. **Not processed** (`route=NOT_PROCESSED`, decision blank): quantity ≤ 0 or non-numeric; plant not in the plant master (e.g. `PUN2` — I did not guess it meant `PUN1`); unreadable `created_at`; duplicate `(pr_no, pr_line)`. The reason says which.
7. **Item matching**: given `item_code` in master → used. Otherwise description is matched (abbreviation expansion, model numbers must match exactly, light typo tolerance). No convincing match → **NEW** item (never an invented code). A code that is not in the master is treated as untrusted and the description is matched instead.
8. **Missing need-by** falls back to the creation date for clubbing. Missing estimate on a new item values it at ₹0 and says so.
9. **Contracts**: the register is the base; PDF-only contracts (RC-2026-031, RC-2026-033) are read from the PDF and added with consumed value 0. A contract only applies if valid on the requirement date **and** its supplier is Active (RC-2026-005's supplier is Blocked → ignored). Ceiling is consumed in requirement-date order, only by requirements that actually go to CONTRACT_PO; approved POs add to consumption in the live app.

## Decision details
10. **Clubbing**: same item + plant; line joins the earliest open requirement if created ≤ 14 *calendar days* after its first line and need-by within ±7 days of the first line's. Verified against worked examples 4 and 14.
11. **Value (3.1)** is computed with contract rate → lowest applicable contract rate if all are exhausted → latest PO price (any supplier, 365 days) → standard price. The **PO price** for SPOT/EMERGENCY is the chosen supplier's *reference price* (its latest PO in 548 days), which can differ from the valuation price (worked example 6 shows exactly this).
12. History windows are inclusive of the as-of date (`as_of − N ≤ po_date ≤ as_of`). POs created by this app are not fed back into history.
13. **Preferred ranking** (policy leaves weights open): `0.30·frequency(PO count capped at 5) + 0.25·recency(1 / 0.5 / 0 for last PO ≤90d / ≤365d / older) + 0.15·contract held + 0.15·on-time + 0.15·quality`. Ties: more recent PO, then supplier id. Weights live in the policy JSON. This reproduces the ordering of the worked examples.
14. **Sourcing suggestions**: preferred list first, topped up with other Active suppliers of the item's category ranked by 0.5·on-time + 0.5·quality; fewer than N if fewer exist (as in the encoder example).
15. **Consolidation**: PO requirements with same supplier + plant + route whose first lines are within 24 h of the PO's first requirement share a PO. `PO-nnnn`/`REQ-nnnn` ids are run-local and numbered in creation order; real document numbers (`PO-26-0001`) are issued on approval. (The kit's worked-example PO numbers are off by one versus my output because PR-26-00174/175, created at 08:10 on 2 Nov, are the earliest requirement.)
16. **Override**: needs a written reason, keeps the original decision, and can never name a Blocked/Inactive supplier (5.7 holds even for overrides).

## Product / engineering
17. **Auth is simulated**: a role switcher (requester / buyer / procurement) sends `X-User`/`X-Role` headers; roles are enforced server-side for approvals, uploads and policy changes. A real deployment would put SSO in front.
18. **Live queue**: chat PRs go into one rolling batch that is re-evaluated on every new PR so clubbing sees them. Approved lines leave the queue (the PO exists), so a later PR becomes a new requirement. Known limitation.
19. **Task data is not in the repo**: load it at runtime from the Setup tab (zip or CSVs/PDFs; files are recognised by columns) or set `SEED_DATA_DIR` / `SEED_DATA_ZIP` / `SEED_DATA_URL`.
20. The email says "Wed 19 Nov" and "Thu 19 Nov" for the same deadline (19 Nov 2026 is a Thursday); I planned for Thu 19 Nov 10:00 IST.
