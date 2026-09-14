# Private pilot image and health verification

The `containers` CI job builds the two actual deployment Dockerfiles on a disposable Linux runner. It validates the resolved Compose configuration using synthetic credentials, checks the private loopback binding and paper-only setting, and exercises both images with a new shared volume. Image IDs, checkout revision, PR head, dependency versions and each result are retained in the `pilot-container-verification` CI artifact.

The runtime containers use `--network none` and UID 10001. The Python fixture is mounted from `tests/`; it calls the real paper broker, protection tick and telemetry publisher with two synthetic INFY shares and generated ticks. It does not start provider streams or the analysis cadence. The UI starts through its production image entrypoint and reads the same SQLite state as the engine. No deployment credentials, broker sessions or existing volumes are used.

The check covers:

- Installation/import of the actual pilot SDK dependencies and both production image builds.
- Private unauthenticated rejection, login, CSRF rejection and no-store authenticated workspace responses.
- Shared account/holdings/market reads and paper-only reporting. Missing strategy and recovery acceptance stays failed.
- Saved watchlist mutation, retrieval and persistence across dashboard restart.
- A dashboard halt request acknowledged by protection; persistence across engine restart with a heartbeat newer than the restarted container’s start time; explicit operator-command recovery.
- Stale heartbeat rejection after fixture publication is frozen, followed by fresh-observation recovery.
- Cleanup of the disposable containers, data volume and images, recorded in the artifact.

Run on a Docker-capable development host with a **new** output path:

```sh
python scripts/verify_pilot_containers.py --output /tmp/new-pilot-container-verification.json
```

Builds require package-registry access. The script runs only the containers/volume it creates and never starts the configured production Compose stack. Do not treat the fixture as a deployment mode. It does not exercise the actual collector, websocket connection, AI analysis, exchange execution, market-session timing, independent alert receipt, encrypted off-host recovery or full Compose startup. Those remain target-host acceptance requirements.

## Local protection health contract

`python scripts/pilot_ops.py health --database /path/to/ledger --tenant TENANT` now checks the persisted row **and** its embedded engine payload. Both timestamps must be timezone-aware, represent the same instant, and fall between 5 seconds ahead and 15 seconds behind the checking clock. A recent database timestamp cannot conceal stale or malformed engine evidence. The payload must explicitly say `mode: paper`, `status: running` and contain a boolean halt state. Payload parsing is bounded; missing/unreadable storage, absent heartbeat, invalid types and corrupt/oversized JSON fail closed.

- Exit 0 / `status: observation_ok`: a fresh, consistent paper protection heartbeat with no reported halt.
- Exit 2 / `status: unhealthy`: missing, invalid, stale, stopped or halted evidence. A valid halted engine retains `heartbeat: ok` and reports `engine_halted`; its protection loop remains live.

Output contains bounded reason codes, the original observation time, ages and halt state, never raw engine contents, balances or the operator's halt reason. The reader does not create a missing database, write application state, clear a halt or contact a provider. SQLite can manage its normal read-side WAL/SHM files.

This changes the old probe's behavior: previously a fresh row timestamp alone passed and an active halt exited successfully. Monitoring should treat a halt as an availability event and handle planned maintenance explicitly. Do not connect this result to automatic resume or restart actions. Protection must continue while entries are halted. This is an observation of persisted evidence, not proof of a healthy process, source freshness, current positions or strategy effectiveness.

The independent hosted monitor retains its separate credential and 150-second original-heartbeat limit; see [monitoring contract](INDEPENDENT_MONITOR.md). Its slower publication cadence must not be substituted for the local 15-second probe.

## Verification status

The verifier now uses the resolved Compose environment for both runtime containers, loads three actual published research/event fixtures, honors a custom directives mount and checks authenticated exports. Missing configured files must fail closed without breaking the other panels, and restoration is checked. [Deployment wiring and precise limits](DEPLOYMENT_WIRING.md). The collector's custom calendar is covered by deterministic tests; it is not started against a provider in this CI fixture.

Local Python tests and the synthetic engine/dashboard prerequisite verify the health rejection cases, private login, shared account, saved watchlist, halt acknowledgement, engine halt persistence and dashboard restart. Exact image-build and Linux container results are recorded by CI after the job runs. A green CI artifact qualifies packaging and the listed isolated flows only; intended-host deployment, real-session observation and operational burn-in remain open.

The first Linux CI run built both images and passed 11 recorded checks. Artifact review found that the restart assertion could still read the prior heartbeat; the check now requires an observation at or after Docker's recorded `StartedAt` for the new container process. This prevents pre-restart evidence from satisfying restart acceptance. Inspect the artifact's `haltAfterEngineRestart.observed_at` and `restartedContainerStartedAt` fields.


The recorded-account benchmark phase creates a separate six-fill synthetic account through the real paper broker and starts another dashboard instance from the same production image. Runtime networking remains disabled. It checks 31 daily marks against an independent oracle, all six benchmark/window exports, authentication, no-store headers and saved bounded Atlas context with no provider key. The original protection/ledger fixture remains separate. Inspect the `account-benchmark` phase in the revision-specific artifact.


The `broker-observation` phase starts a separate dashboard container using a selected synthetic account capture. It checks authentication, 14 orders/26 executions, private no-store export, changing/mismatched/stale/wrong-account handling and bounded saved Atlas context. Both broker and model external calls remain zero. It does not qualify an actual external broker account or target deployment.
