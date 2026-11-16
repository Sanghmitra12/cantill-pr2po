# APPROACH.md

## What it is
A FastAPI + SQLite backend with a no-build vanilla-JS frontend. Requesters chat to raise PRs; the same engine processes CSV batches; buyers approve/override; Procurement changes the policy by uploading or describing an amendment.

## Architecture (all in `backend/app`)
- `policy.py` — policy schema, validation, condition evaluator, plain-language rendering and diffing. **The only place rules are interpreted.**
- `engine.py` — pure function: PR rows → resolved items → clubbed requirements → contract/valuation/facts → first matching rule → supplier selection → PO consolidation. No DB/web imports; unit-tested.
- `suppliers.py` — preferred lists, ranking, reference prices, suggestions.
- `matching.py` — explainable item matcher (never invents items).
- `policy_nl.py` / `llm.py` — amendment interpretation: LLM when a key is configured, deterministic rules otherwise; output is always validated, diffed and held as a draft until approved.
- `chat.py` — slot-filling assistant (item → quantity → plant → need-by → urgency → confirm), asks only for what is missing, lists candidates instead of guessing.
- `service.py`/`db.py`/`main.py` — persistence, audit log, approvals, REST API.

## Policy changes without code
The policy PDF is encoded once as JSON. A new version is a new JSON row; the engine reads the version in force for each requirement's date. The UI shows the diff ("Spot PO limit ₹2,00,000 → ₹3,00,000", "new rule at position 3") before approval, and every approval is in the audit log.

## Verification
`pytest` (synthetic fixtures) and, with `KIT_DIR` set, the worked examples from the kit (values, suppliers and routes match). The matcher resolves every item description in the sample.

## AI tools used
- **Claude (Anthropic)** via Claude Code — wrote most of the code, tests and docs under my direction; I reviewed decisions against the brief.
- **Inside the app**: optional Claude API calls (`llm.py`) for (a) reading policy amendments into the policy JSON and (b) extracting fields from unusual chat messages. Both are optional; without a key the app uses deterministic rules. Item matching and all decisions are deterministic code, never LLM output.
