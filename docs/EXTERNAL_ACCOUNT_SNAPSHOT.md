# Selected external account funds and net positions

The GET-only capture checks the selected Kite profile before and after two funds and two net-position reads, within a 30-second Asia/Kolkata day interval. Malformed account, quantities, amounts or timestamps fail. Changing reads are labelled `changing`; missing cash or available balance is `incomplete`. The report carries both reads, a source digest and a pseudonymous account reference, with no literal account ID, profile contact details or broker token.

For an independently selected account and a valid read session:

```sh
python scripts/capture_external_account.py --account-id EXPECTED_KITE_USER_ID --tenant-id ghost --output /data/reviews/external-account-001.json
```

Set `KITE_API_KEY` and `KITE_ACCESS_TOKEN` in the private process environment. The command creates a mode-0600 file and refuses overwrites. A changing or incomplete report is retained but exits 2. Do not store credentials in the report or repository. The CLI makes only GET requests and does not renew a session.

The account remains separate from the paper ledger. This capture cannot detect a matched paper/live position or cash discrepancy because the accounts are different. Kite net positions exclude a depository holdings inventory; settlement, fees, corporate actions, exhaustive completeness and broker truth are unverified. Hashes detect changed local bytes, not provider authenticity. This is a building block for continuous selected-account reconciliation, which still needs independent holdings/cash evidence and target-host observation. It does not satisfy X01/X02, full QuantConnect reconciliation or real-money activation.

The private Activity panel now reads a selected report through an independent Node validator. Set `PRAMANA_EXTERNAL_ACCOUNT_SNAPSHOT` to the container-visible report path and `PRAMANA_EXTERNAL_ACCOUNT_REF` to its reviewed pseudonymous `accountRef`. Compose forwards both. The panel and authenticated `/api/broker/external-account` export show repeated-read status, age, equity funds and net positions. Atlas receives only status, timestamp and count; cash, position quantities and account reference are omitted. The Cloudflare snapshot publisher removes this private field. Store the report in the already inventoried `/data/reviews` directory or explicitly list its location in the recovery bundle. Synthetic container/browser verification is tracked separately from real selected-account acceptance.
