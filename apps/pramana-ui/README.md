# Pramana Executive Command Center

Read-only Next.js dashboard for the Pramana paper-trading engine.

## Local data

The server reads only:

- `PRAMANA_LEDGER_PATH` (default `../../pramana_ledger.sqlite`)
- `PRAMANA_PROOF_DIR` (default `../../pramana-proofs`)
- `PRAMANA_TENANT_ID` (default `default`)
- `PRAMANA_DECISION_QUALITY_REPORT` (default `decision-quality.json` beside the ledger)
- `PRAMANA_POST_MORTEM_DIR` (default `post-mortems/` beside the ledger; read-only, approval stays on the host CLI)

SQLite is opened with `readonly: true`, `fileMustExist: true`, and `PRAGMA query_only=ON`.
No API route mutates the ledger, proof files, risk parameters, or orders.

## Commands

```bash
npm install
npm run dev
npm run build
```

The UI refreshes GET-only API data every 15 seconds.

## MtM caveat

The current engine does not persist a dedicated market-mark table in SQLite. Open positions are therefore explicitly labeled `ledger_marked`: the latest paper-ledger fill for the symbol is used as the mark, falling back to average entry. The dashboard does not claim this is a live market price.
