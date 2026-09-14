# Independent pilot engine observation

The Cloudflare Worker exposes `GET /healthz` and `HEAD /healthz` for an external uptime monitor. This route checks persisted evidence on Cloudflare, independently of the Mac engine and publisher. It cannot place orders or clear a halt.

## Credentials and response

Provision a separate Worker secret named `MONITOR_TOKEN` through the deployment secret mechanism. Use a cryptographically random value, at least 32 characters. Do not reuse `VIEW_AUTH` or `PUBLISH_TOKEN`, put it in a URL, commit it, or publish it in a dashboard. The monitor sends `Authorization: Bearer <MONITOR_TOKEN>` over HTTPS. A missing/incorrect credential returns 401. The monitor credential cannot read portfolio endpoints, assets or ingest snapshots.

A successful GET returns HTTP 200 with `status: observation_ok`. Missing or unhealthy evidence and D1 errors return HTTP 503. Responses are not cached. HEAD returns the same health status without a body when storage reads succeed. Payloads include only bounded reason codes, timestamps/ages and paper mode; no holdings, balances, operator halt reasons, provider keys or conversation data.

All of these conditions must pass:

- Publication source and cloud receipt timestamps are no more than 180 seconds old and no more than 5 seconds in the future.
- Original `workspace.runtime.updatedAt` is no more than 150 seconds old, allowing the existing 60-second publisher cadence; uploading a stale engine heartbeat cannot reset its age.
- The workspace identifies `india-paper`, runtime mode is `paper`, status is `running`, and halt state is explicitly false.

An engaged halt deliberately produces a failing probe, including a voluntary operator halt. Planned maintenance should be handled in the independent monitor, without modifying engine evidence to appear healthy. Snapshot liveness does not prove current market-feed quality, broker protection, strategy readiness or profitability.

## Target-host acceptance drill (pending)

1. Deploy the reviewed Worker revision and provision the independent credential. Configure an external HTTPS monitor, outside the engine host, with a bounded timeout and alerting for non-200, connection failure and timeout. Keep its alert recipient private.
2. Record a baseline successful probe against the real deployment. Check host clock synchronization.
3. In an explicitly isolated paper pilot, stop the engine while leaving publication running. Confirm /healthz fails even if uploads continue. Also stop publication and confirm stale observations fail. With continuous publication, the UI reader may already label the heartbeat stale after 10 seconds; the independent age cutoff remains 150 seconds if publication stops.
4. Confirm a notification is actually received outside the engine host. Record failure onset, first failing probe, receipt time and recovery. With a 60-second monitor poll, the 150-second heartbeat cutoff implies an approximately 210-second worst-case detection interval before notification delivery, excluding failures or scheduling delays in the monitoring service. Agree this budget explicitly; it is not a sub-second trading protection mechanism.
5. Restore the engine, confirm a new original heartbeat and current publisher evidence, and verify recovery notification and acknowledgement. Run halt and monitor-credential-rotation cases separately.

No external monitor, notification recipient or remote deployment was configured by this change. Do not mark the independent-alert acceptance gate passed from source code or a local drill alone.

## Local verification

Worker tests cover authentication separation, stale heartbeat behind fresh publication, missing/invalid/future timestamps, halt states and storage failure. A local Worker/D1 integration drill ingested synthetic snapshots: no snapshot 503; healthy evidence 200; fresh publication with a stale engine 503; halt 503; recovered evidence 200. This verifies HTTP/storage wiring, not an actually received external alert or target deployment.
