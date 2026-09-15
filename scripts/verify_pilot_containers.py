"""Build and exercise disposable paper/UI images without external runtime networking.

Requires Docker. Does not use deployment secrets or a production volume. The engine
fixture runs protection/telemetry only, with synthetic ticks and no provider calls.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, input=None, expected=0, timeout=90):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != expected:
        # Inputs/arguments are all synthetic; never read host credential files.
        raise RuntimeError(f"{args[0:2]} exited {result.returncode}: {result.stdout[-3000:]} {result.stderr[-3000:]}")
    return result.stdout.strip()


def wait_for(check, label, seconds=40):
    until = time.monotonic() + seconds
    last = None
    while time.monotonic() < until:
        try:
            return check()
        except (RuntimeError, ValueError) as error:
            last = error
            time.sleep(.5)
    raise RuntimeError(f"Timed out waiting for {label}: {last}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Verification output must be new")
    revision = run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    name = "pramana-qa-" + uuid.uuid4().hex[:12]
    engine, ui, volume = name + "-engine", name + "-ui", name + "-data"
    image_engine, image_ui = name + ":engine", name + ":ui"
    report = {"schema":"pramana.container_verification.v1", "revision":revision,
              "pullRequestHead":os.environ.get("PRAMANA_CI_HEAD_SHA"),
              "startedAt":datetime.now(timezone.utc).isoformat(), "status":"running", "checks":[],
              "scope":"Disposable Linux containers and synthetic paper state; not target-host acceptance",
              "externalProviderCalls":0, "realOrders":0, "deployments":0}
    created_containers, created_images = [], []
    volume_created = False
    fixture_directory = tempfile.TemporaryDirectory(prefix="pramana-deployment-fixture-")
    try:
        directives_file = Path(fixture_directory.name) / "reviewed-directives.json"
        directives = json.loads((ROOT / "deploy/founder-directives.example.json").read_text())
        # The synthetic tick fixture feeds one instrument, so narrow the shipped example
        # watchlist to it. A watchlist symbol with no websocket mapping is permanently
        # vetoed as missing market data, which would fail the feed check for reasons that
        # have nothing to do with the container build this job exists to verify.
        directives.update(
            starting_capital=123456,
            max_open_positions=3,
            watchlist=[item for item in directives["watchlist"] if item["symbol"] == "INFY"],
        )
        directives_file.write_text(json.dumps(directives))
        directives_file.chmod(0o644)  # Synthetic data read by UID 10001 through a single-file mount.
        external_fixture = json.loads((ROOT / "tests/fixtures/external-account-ghost.json").read_text())
        attribution_config = {"PRAMANA_EXTERNAL_ACCOUNT_SNAPSHOT":"/qa-external-account.json",
                              "PRAMANA_EXTERNAL_ACCOUNT_REF":external_fixture["accountRef"],
                              "PRAMANA_BENCHMARK_ATTRIBUTION_REPORT":"/qa-benchmark-attribution.json",
                              "PRAMANA_BENCHMARK_ATTRIBUTION_PORTFOLIO":"example-portfolio"}
        research_paths = {"PRAMANA_RESEARCH_LAB_REPORT":"/data/research/comparison-001.json",
                          "PRAMANA_PORTFOLIO_RESEARCH_REPORT":"/data/research/portfolio-001.json",
                          "PRAMANA_RESEARCH_DASHBOARD_SNAPSHOT":"/data/research/dashboard-snapshot.json",
                          "PRAMANA_COMPANY_EVENTS_DB":"/data/research/events.sqlite"}
        safe_environment = {key:value for key, value in os.environ.items()
                            if not key.startswith(("PRAMANA_", "ANTHROPIC_", "ZERODHA_", "FRED_", "TRADING_"))}
        compose_env = {**safe_environment, "ANTHROPIC_API_KEY":"synthetic", "ZERODHA_API_KEY":"synthetic",
                       "ZERODHA_ACCESS_TOKEN":"synthetic", "PRAMANA_ZERODHA_TOKENS_JSON":"[1]",
                       "PRAMANA_ZERODHA_SYMBOLS_JSON":'{"1":"INFY"}',
                       "PRAMANA_DASHBOARD_SECRET":"synthetic-container-smoke-session-key-only",
                       "PRAMANA_PUBLIC_ORIGIN":"http://localhost:3000", "PRAMANA_RELEASE_REVISION":revision,
                       "PRAMANA_HOLIDAYS_JSON":'{"INDIA":["2026-09-11"]}',
                       "PRAMANA_DIRECTIVES_HOST_FILE":str(directives_file), **research_paths, **attribution_config}
        rendered = subprocess.run(["docker", "compose", "--env-file", str(ROOT / ".env.example"),
                                   "-f", str(ROOT / "deploy/docker-compose.yml"), "config", "--format", "json"],
                                  env=compose_env, capture_output=True, text=True, check=True, timeout=30)
        services = json.loads(rendered.stdout)["services"]
        for service in services.values():
            assert service["environment"]["TRADING_LIVE_MONEY_ACTIVE"] == "false"
            assert service["environment"]["PRAMANA_LEDGER_PATH"] == "/data/pramana.db"
        assert services["dashboard"]["ports"][0]["host_ip"] == "127.0.0.1"
        assert services["pramana-ghost"]["environment"]["PRAMANA_AI_BUDGET_DB"] == "/data/ai-budget.sqlite"
        assert services["pramana-ghost"]["environment"]["PRAMANA_FUNDAMENTALS_PROVIDER"] == "yahoo"
        assert services["dashboard"]["environment"]["PRAMANA_CHAT_DAILY_LIMIT"] == "200"
        for service in ("pramana-ghost", "dashboard"):
            assert services[service]["environment"]["PRAMANA_DECISION_QUALITY_REPORT"] == "/data/decision-quality.json"
            assert services[service]["environment"]["PRAMANA_POST_MORTEM_DIR"] == "/data/post-mortems"
        assert services["pramana-ghost"]["healthcheck"]["test"][3] == "health"
        for key, value in {**research_paths, **attribution_config}.items():
            assert services["dashboard"]["environment"][key] == value
        assert services["market-monitor"]["environment"]["PRAMANA_HOLIDAYS_JSON"] == services["pramana-ghost"]["environment"]["PRAMANA_HOLIDAYS_JSON"] == compose_env["PRAMANA_HOLIDAYS_JSON"]
        directives_mount = next(v for v in services["pramana-ghost"]["volumes"] if v["target"] == "/app/directives.json")
        assert directives_mount["source"] == str(directives_file) and directives_mount["read_only"]
        # Operational blindness is a deployment fault: alerts must be durable, logs bounded,
        # memory capped and backups scheduled, in the rendered configuration itself.
        assert services["pramana-ghost"]["environment"]["PRAMANA_ALERT_LOG"] == "/data/alerts.jsonl"
        for optional in ("PRAMANA_TELEGRAM_BOT_TOKEN", "PRAMANA_TELEGRAM_CHAT_ID"):
            assert services["pramana-ghost"]["environment"][optional] == ""
        limits = {}
        for name, service in services.items():
            assert service["logging"]["driver"] == "json-file"
            assert service["logging"]["options"] == {"max-size":"10m", "max-file":"5"}
            # Every service is capped, and no single cap exceeds a small instance's memory.
            # Compose renders the limit as a byte count, as a string in some versions.
            limits[name] = int(service["mem_limit"])
            assert 0 < limits[name] <= 1024 ** 3
        backup_service = services["backup"]
        assert backup_service["entrypoint"] == ["python", "/app/scripts/scheduled_backup.py"]
        assert backup_service["command"] == ["--database", "/data/pramana.db", "--directory", "/data/backups",
                                             "--interval-seconds", "86400", "--keep", "14"]
        report["checks"].append({"composeConfiguration":"pass", "liveEnabled":False, "dashboardBind":"loopback",
                                 "researchPaths":research_paths, "customDirectivesMount":"read_only",
                                 "memoryLimitBytes":limits, "logRotation":{"max-size":"10m", "max-file":"5"},
                                 "durableAlertLog":"/data/alerts.jsonl",
                                 "scheduledBackup":{"command":backup_service["command"], "schedule":"daily"}})
        # Render the optional private ingress with synthetic credentials only.
        token_file = Path(fixture_directory.name) / "tunnel-token"
        token_file.write_text("synthetic-never-connect")
        token_file.chmod(0o600)
        tunnel_env = {**compose_env,
                      "PRAMANA_CLOUDFLARED_IMAGE": "cloudflare/cloudflared@sha256:" + "a" * 64,
                      "PRAMANA_TUNNEL_TOKEN_FILE": str(token_file)}
        tunnel_command = ["docker", "compose", "--env-file", str(ROOT / ".env.example"),
                          "-f", str(ROOT / "deploy/docker-compose.yml"),
                          "-f", str(ROOT / "deploy/docker-compose.cloudflare.yml"),
                          "config", "--format", "json"]
        tunnel_rendered = subprocess.run(tunnel_command, env=tunnel_env, capture_output=True,
                                         text=True, check=True, timeout=30)
        tunnel_config = json.loads(tunnel_rendered.stdout)
        tunnel = tunnel_config["services"]["cloudflare-tunnel"]
        assert not tunnel.get("ports") and not tunnel.get("volumes")
        assert not tunnel.get("environment")
        assert tunnel["read_only"] is True and "ALL" in tunnel["cap_drop"]
        assert tunnel["command"] == ["tunnel", "--no-autoupdate", "run", "--token-file",
                                      "/run/secrets/cloudflare_tunnel_token"]
        assert len(tunnel["secrets"]) == 1
        assert tunnel["secrets"][0]["source"] == "cloudflare_tunnel_token"
        assert tunnel["secrets"][0]["target"] == "cloudflare_tunnel_token"
        assert tunnel_config["secrets"]["cloudflare_tunnel_token"]["file"] == str(token_file)
        for service_name, service in services.items():
            assert tunnel_config["services"][service_name] == service
        for required in ("PRAMANA_CLOUDFLARED_IMAGE", "PRAMANA_TUNNEL_TOKEN_FILE"):
            incomplete = {key: value for key, value in tunnel_env.items() if key != required}
            rejected = subprocess.run(tunnel_command, env=incomplete, capture_output=True,
                                      text=True, check=False, timeout=30)
            assert rejected.returncode != 0, f"Missing {required} was accepted"
        report["checks"].append({"cloudflareComposeConfiguration": "pass",
                                 "scope": "Configuration only; no tunnel connection or Access policy tested",
                                 "missingSettingsRejected": True, "additionalPublishedPorts": 0})
        for dockerfile, image in [("Dockerfile", image_engine), ("Dockerfile.ui", image_ui)]:
            print(f"Building {dockerfile}", flush=True)
            subprocess.run(["docker", "build", "--label", f"org.opencontainers.image.revision={revision}",
                            "-f", str(ROOT / "deploy" / dockerfile), "-t", image, str(ROOT)], check=True, timeout=1200)
            created_images.append(image)
        run("docker", "volume", "create", volume)
        volume_created = True
        shared = ["--network", "none", "--mount", f"type=volume,src={volume},dst=/data",
                  "--mount", f"type=bind,src={ROOT / 'tests'},dst=/qa,readonly"]

        def environment(service, **overrides):
            # Exercise the rendered Compose values; suppress paid provider access in the fixture.
            values = {**services[service]["environment"], "ANTHROPIC_API_KEY":"", **overrides}
            return [part for key, value in values.items() for part in ["-e", f"{key}={value or ''}"]]

        run("docker", "run", "-d", "--name", engine, *shared, *environment("pramana-ghost"),
            "--mount", f"type=bind,src={directives_mount['source']},dst={directives_mount['target']},readonly",
            "--entrypoint", "python", image_engine,
            "/qa/container_runtime_fixture.py", "--state", "/data")
        created_containers.append(engine)
        health_command = ["docker", "exec", engine, "python", "/app/scripts/pilot_ops.py", "health",
                          "--database", services["pramana-ghost"]["environment"]["PRAMANA_LEDGER_PATH"],
                          "--tenant", services["pramana-ghost"]["environment"]["PRAMANA_TENANT_ID"]]

        def health(expected=0):
            return json.loads(run(*health_command, expected=expected))

        report["checks"].append({"initialEngineHealth":wait_for(health, "engine heartbeat")})
        # The backup service's own command against the live ledger: one cycle must leave a
        # verified copy plus its manifest, taken while the engine keeps writing.
        run("docker", "exec", engine, "python", "/app/scripts/scheduled_backup.py",
            "--database", services["pramana-ghost"]["environment"]["PRAMANA_LEDGER_PATH"],
            "--directory", "/data/backups", "--keep", "14", "--once")
        report["checks"].append(json.loads(run("docker", "exec", engine, "python", "-c",
            "import hashlib,json; from pathlib import Path; "
            "copies=sorted(Path('/data/backups').glob('pramana-*.db')); assert len(copies)==1, copies; "
            "manifest=json.loads(Path(str(copies[0])+'.manifest.json').read_text()); "
            "assert manifest['integrity']=='ok' and manifest['sha256']==hashlib.sha256(copies[0].read_bytes()).hexdigest(); "
            "print(json.dumps({'scheduledBackupCycle':'pass', 'backup':manifest['backup'], "
            "'integrity':manifest['integrity'], 'engineInterrupted':False}))")))
        sdk = run("docker", "exec", engine, "python", "-c",
                  "import kiteconnect, ib_async, certifi; from importlib.metadata import version; "
                  "import json; print(json.dumps({p:version(p) for p in ['kiteconnect','ib_async','certifi']}))")
        report["pilotDependencies"] = json.loads(sdk)
        report["checks"].append(json.loads(run("docker", "exec", engine, "python", "/qa/kite_timestamp_smoke.py")))
        run("docker", "run", "-d", "--name", ui, *shared, *environment("dashboard"),
            "--mount", f"type=bind,src={ROOT / 'apps/pramana-ui/tests/fixtures/benchmark-attribution.json'},dst=/qa-benchmark-attribution.json,readonly",
            "--mount", f"type=bind,src={ROOT / 'tests/fixtures/external-account-ghost.json'},dst=/qa-external-account.json,readonly", image_ui)
        created_containers.append(ui)

        def ready():
            return run("docker", "exec", ui, "node", "-e",
                       "fetch('http://localhost:3000/login').then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))")

        def dashboard(phase):
            result = run("docker", "exec", "-e", f"SMOKE_PHASE={phase}", "-e", "SMOKE_SESSION_FILE=/data/smoke-session", ui,
                         "node", "/qa/container_dashboard_smoke.mjs")
            report["checks"].append(json.loads(result))

        def halt_health():
            result = health(2)
            if result["reasons"] != ["engine_halted"] or result["heartbeat"] != "ok":
                raise ValueError("Halt has not been acknowledged")
            return result

        wait_for(ready, "private dashboard")
        for container in [engine, ui]:
            assert run("docker", "exec", container, "id", "-u") == "10001"
        dashboard("initial")
        report["checks"].append({"acknowledgedHalt":wait_for(halt_health, "halt acknowledgement")})
        dashboard("halted")
        # Restart while halted: the persisted request and risk state must survive.
        run("docker", "restart", engine)
        started_at = run("docker", "inspect", "--format", "{{.State.StartedAt}}", engine)

        def restarted_halt():
            result = halt_health()
            if datetime.fromisoformat(result["observed_at"]) < datetime.fromisoformat(started_at.replace("Z", "+00:00")):
                raise ValueError("Waiting for a heartbeat written by the restarted container")
            return result

        report["checks"].append({"haltAfterEngineRestart":wait_for(restarted_halt, "new halted heartbeat after restart"),
                                 "restartedContainerStartedAt":started_at})
        run("docker", "exec", engine, "pramana", "resume")
        wait_for(health, "operator-file halt recovery")
        dashboard("recovered")
        run("docker", "restart", ui)
        wait_for(ready, "dashboard restart")
        dashboard("restarted")
        # A selected source disappearing must fail closed, while other panels stay usable.
        for withheld in [True, False]:
            run("docker", "exec", engine, "python", "-c",
                "import sys,json; from pathlib import Path; "
                "paths=[Path(p) for p in json.loads(sys.argv[1])]; "
                + ("[(p.rename(str(p)+'.withheld')) for p in paths]" if withheld
                   else "[Path(str(p)+'.withheld').rename(p) for p in paths]"),
                json.dumps(list(research_paths.values())))
            dashboard("missing-research" if withheld else "research-restored")
        # Freeze fixture publication, then age the persisted observation explicitly.
        run("docker", "exec", engine, "python", "-c",
            "from pathlib import Path; Path('/data/fixture-mode').write_text('freeze')")

        def frozen():
            return run("docker", "exec", engine, "python", "-c",
                       "from pathlib import Path; assert Path('/data/fixture-observed-mode').read_text()=='freeze'")

        wait_for(frozen, "fixture freeze")
        run("docker", "exec", engine, "python", "-c",
            "import sqlite3,json,os; db=sqlite3.connect(os.environ['PRAMANA_LEDGER_PATH']); "
            "p=json.loads(db.execute('SELECT payload FROM pilot_runtime').fetchone()[0]); "
            "p['updatedAt']='2000-01-01T00:00:00+00:00'; "
            "db.execute('UPDATE pilot_runtime SET updated_at=?,payload=?',(p['updatedAt'],json.dumps(p))); db.commit(); db.close()")
        stale = health(2)
        assert "payload_heartbeat_stale_or_invalid" in stale["reasons"]
        report["checks"].append({"staleHeartbeat":stale})
        dashboard("stale")
        run("docker", "exec", engine, "python", "-c",
            "from pathlib import Path; Path('/data/fixture-mode').write_text('fresh')")
        report["checks"].append({"recoveredHeartbeat":wait_for(health, "fresh observation recovery")})
        # A healthy heartbeat is not protection coverage. Exercise legacy missing
        # protection, its durable fault halt, and explicit fixture repair on real images.
        def set_stop(value):
            run("docker", "exec", engine, "python", "-c",
                "import sqlite3,os,json,sys; db=sqlite3.connect(os.environ['PRAMANA_LEDGER_PATH']); "
                "db.execute('UPDATE paper_positions SET stop_price=? WHERE tenant_id=?', "
                "(json.loads(sys.argv[1]),os.environ['PRAMANA_TENANT_ID'])); db.commit(); db.close()", json.dumps(value))

        def protection_state(expected):
            state = json.loads(run("docker", "exec", engine, "python", "-c",
                "import sqlite3,os; db=sqlite3.connect(os.environ['PRAMANA_LEDGER_PATH']); "
                "print(db.execute('SELECT payload FROM pilot_runtime WHERE tenant_id=?', "
                "(os.environ['PRAMANA_TENANT_ID'],)).fetchone()[0]); db.close()"))
            if (state["protectionCoverage"]["status"] != expected or not state["halted"]
                    or state["haltReason"] != "paper_position_protection_incomplete"):
                raise ValueError("Waiting for protection coverage and durable halt")
            return {"coverage":state["protectionCoverage"], "health":halt_health()}

        set_stop(None)
        report["checks"].append({"missingProtection":wait_for(lambda: protection_state("incomplete"), "missing protection halt")})
        dashboard("missing-protection")
        run("docker", "restart", engine)
        started_at = run("docker", "inspect", "--format", "{{.State.StartedAt}}", engine)
        new_heartbeat = wait_for(restarted_halt, "new protection-fault heartbeat after restart")
        report["checks"].append({"protectionHaltAfterRestart":protection_state("incomplete"),
                                 "restartHeartbeat":new_heartbeat, "restartedContainerStartedAt":started_at})
        dashboard("protection-restarted")
        set_stop("95")
        report["checks"].append({"restoredProtectionHalted":wait_for(lambda: protection_state("complete"), "restored coverage without auto-resume")})
        dashboard("protection-restored")
        original_average = run("docker", "exec", engine, "python", "-c",
            "import sqlite3,os; db=sqlite3.connect(os.environ['PRAMANA_LEDGER_PATH']); "
            "print(db.execute('SELECT average_price FROM paper_positions WHERE tenant_id=?', "
            "(os.environ['PRAMANA_TENANT_ID'],)).fetchone()[0]); db.close()")

        def set_average(value):
            run("docker", "exec", engine, "python", "-c",
                "import sqlite3,os,sys; db=sqlite3.connect(os.environ['PRAMANA_LEDGER_PATH']); "
                "db.execute('UPDATE paper_positions SET average_price=? WHERE tenant_id=?', "
                "(sys.argv[1],os.environ['PRAMANA_TENANT_ID'])); db.commit(); db.close()", value)

        def valuation_state(expected):
            state = json.loads(run("docker", "exec", engine, "python", "-c",
                "import sqlite3,os,json; db=sqlite3.connect(os.environ['PRAMANA_LEDGER_PATH']); "
                "tenant=os.environ['PRAMANA_TENANT_ID']; "
                "runtime=json.loads(db.execute('SELECT payload FROM pilot_runtime WHERE tenant_id=?',(tenant,)).fetchone()[0]); "
                "valuation=json.loads(db.execute('SELECT payload FROM paper_live_valuations WHERE tenant_id=? ORDER BY timestamp DESC LIMIT 1',(tenant,)).fetchone()[0]); "
                "print(json.dumps({'runtime':runtime['valuation'],'snapshotStatus':valuation['status'],'equity':valuation['totalEquity'],'halted':runtime['halted']})); db.close()"))
            if state["runtime"]["status"] != expected or not state["halted"]:
                raise ValueError("Waiting for requested valuation state with fault halt")
            if expected == "unavailable" and (state["snapshotStatus"] != "invalid" or state["equity"] is not None):
                raise ValueError("Invalid valuation must withhold equity")
            if expected == "available" and state["snapshotStatus"] != "ok":
                raise ValueError("Waiting for next valid minute; the invalid minute must remain")
            return state

        set_average("broken")
        report["checks"].append({"invalidLedgerObservation":wait_for(lambda: valuation_state("unavailable"), "invalid ledger observation")})
        dashboard("invalid-ledger")
        run("docker", "restart", engine)
        started_at = run("docker", "inspect", "--format", "{{.State.StartedAt}}", engine)
        new_heartbeat = wait_for(restarted_halt, "new heartbeat with invalid ledger after restart")
        report["checks"].append({"invalidLedgerRestart":valuation_state("unavailable"),
                                 "restartHeartbeat":new_heartbeat, "restartedContainerStartedAt":started_at})
        dashboard("ledger-restarted")
        set_average(original_average)
        report["checks"].append({"restoredLedgerObservation":wait_for(lambda: valuation_state("available"), "restored valuation after invalid minute", seconds=75)})
        dashboard("ledger-restored")
        run("docker", "exec", engine, "python", "-c", "from pathlib import Path; Path('/data/fixture-mode').write_text('bad-ticks')")
        def stream_state():
            state = json.loads(run("docker", "exec", engine, "python", "-c",
                "import sqlite3,os,json; db=sqlite3.connect(os.environ['PRAMANA_LEDGER_PATH']); "
                "r=json.loads(db.execute('SELECT payload FROM pilot_runtime WHERE tenant_id=?',(os.environ['PRAMANA_TENANT_ID'],)).fetchone()[0]); "
                "print(json.dumps(r['marketDataIntegrity'])); db.close()"))
            if not all(state['rejected'].get(k, 0) > 0 for k in ['out_of_order_tick','invalid_tick_values','duplicate_tick','future_tick']):
                raise ValueError("Waiting for stream rejection evidence")
            return state
        report["checks"].append({"streamIntegrityObservation":wait_for(stream_state, "stream rejection observation")})
        dashboard("stream-rejections")
        # Separate recorded account avoids modifying the running protection/ledger drill.
        run("docker", "exec", engine, "python", "/qa/account_benchmark_fixture.py", "--runtime-state", "/data/benchmark")
        benchmark_ui = name + "-benchmark-ui"
        run("docker", "run", "-d", "--name", benchmark_ui, *shared,
            *environment("dashboard", PRAMANA_TENANT_ID="default",
                PRAMANA_LEDGER_PATH="/data/benchmark/paper.sqlite",
                PRAMANA_MARKET_SNAPSHOT="/data/benchmark/market.json",
                PRAMANA_CONSOLE_DB="/data/benchmark/console.sqlite"), image_ui)
        created_containers.append(benchmark_ui)
        wait_for(lambda: run("docker", "exec", benchmark_ui, "node", "-e",
            "fetch('http://localhost:3000/login').then(r=>{if(r.status!==200)process.exit(1)}).catch(()=>process.exit(1))"), "benchmark dashboard startup")
        report["checks"].append(json.loads(run("docker", "exec", benchmark_ui, "node", "/qa/account_benchmark_smoke.mjs")))
        # Isolated broker observation fixture: GET-shaped synthetic evidence, no external account requests.
        captured = json.loads(run("docker", "exec", engine, "python", "/qa/broker_observation_fixture.py", "--output", "/data/benchmark/broker.json"))
        broker_ui = name + "-broker-ui"
        run("docker", "run", "-d", "--name", broker_ui, *shared,
            *environment("dashboard", PRAMANA_TENANT_ID="default",
                PRAMANA_LEDGER_PATH="/data/benchmark/paper.sqlite",
                PRAMANA_MARKET_SNAPSHOT="/data/benchmark/market.json",
                PRAMANA_CONSOLE_DB="/data/benchmark/console.sqlite",
                PRAMANA_BROKER_OBSERVATION="/data/benchmark/broker.json",
                PRAMANA_BROKER_ACCOUNT_REF=captured["accountRef"]), image_ui)
        created_containers.append(broker_ui)
        wait_for(lambda: run("docker", "exec", broker_ui, "node", "-e",
            "fetch('http://localhost:3000/login').then(r=>{if(r.status!==200)process.exit(1)}).catch(()=>process.exit(1))"), "broker dashboard startup")
        report["checks"].append(json.loads(run("docker", "exec", broker_ui, "node", "/qa/broker_observation_smoke.mjs")))
        journal_capture = json.loads(run("docker", "exec", engine, "python", "/qa/broker_journal_fixture.py", "--database", "/data/benchmark/broker-history.sqlite"))
        for phase, database in [("broker-history", "/data/benchmark/broker-history.sqlite"), ("broker-history-restored", "/data/benchmark/broker-restored.sqlite")]:
            if phase == "broker-history-restored":
                recovery = json.loads(run("docker", "exec", engine, "python", "-c",
                    "from pathlib import Path; import json; from quant_ai.operations.recovery_bundle import sqlite_backup; "
                    "from quant_ai.operations.research_recovery import inspect; "
                    "src=Path('/data/benchmark/broker-history.sqlite'); dst=Path('/data/benchmark/broker-restored.sqlite'); "
                    "before=inspect(src,'broker_journal'); sqlite_backup(src,dst); after=inspect(dst,'broker_journal'); "
                    "assert before==after; print(json.dumps({'brokerJournalRecovery':'pass','verification':after}))"))
                report["checks"].append(recovery)
            history_ui = name + "-" + phase
            run("docker", "run", "-d", "--name", history_ui, *shared,
                *environment("dashboard", PRAMANA_TENANT_ID="default",
                    PRAMANA_LEDGER_PATH="/data/benchmark/paper.sqlite",
                    PRAMANA_MARKET_SNAPSHOT="/data/benchmark/market.json",
                    PRAMANA_CONSOLE_DB="/data/benchmark/console.sqlite",
                    PRAMANA_BROKER_OBSERVATION="", PRAMANA_BROKER_JOURNAL=database,
                    PRAMANA_BROKER_ACCOUNT_REF=journal_capture["accountRef"], BROKER_HISTORY_PHASE=phase), image_ui)
            created_containers.append(history_ui)
            wait_for(lambda history_ui=history_ui: run("docker", "exec", history_ui, "node", "-e",
                "fetch('http://localhost:3000/login').then(r=>{if(r.status!==200)process.exit(1)}).catch(()=>process.exit(1))"), "broker history dashboard startup")
            report["checks"].append(json.loads(run("docker", "exec", history_ui, "node", "/qa/broker_journal_smoke.mjs")))
        paired = json.loads(run("docker", "exec", engine, "python", "/qa/run_comparison_fixture.py", "--directory", "/data/run-comparison", "--mature-history"))
        assert paired["points"] == 70 and paired["gaps"] == 3
        for phase, filename in [("run-comparison", "gapped.json"), ("run-comparison-restored", "gapped-restored.json")]:
            if phase == "run-comparison-restored":
                report["checks"].append(json.loads(run("docker", "exec", engine, "python", "/qa/run_comparison_fixture.py", "--directory", "/data/run-comparison", "--restore")))
            paired_ui = name + "-" + phase
            run("docker", "run", "-d", "--name", paired_ui, *shared,
                *environment("dashboard", PRAMANA_TENANT_ID="default",
                    PRAMANA_LEDGER_PATH="/data/run-comparison/paper.sqlite",
                    PRAMANA_MARKET_SNAPSHOT="/data/run-comparison/no-market.json",
                    PRAMANA_CONSOLE_DB="/data/run-comparison/console.sqlite",
                    PRAMANA_RUN_COMPARISON="/data/run-comparison/" + filename,
                    RUN_COMPARISON_PHASE=phase), image_ui)
            created_containers.append(paired_ui)
            wait_for(lambda paired_ui=paired_ui: run("docker", "exec", paired_ui, "node", "-e",
                "fetch('http://localhost:3000/login').then(r=>{if(r.status!==200)process.exit(1)}).catch(()=>process.exit(1))"), "run comparison dashboard startup")
            report["checks"].append(json.loads(run("docker", "exec", paired_ui, "node", "/qa/run_comparison_smoke.mjs")))
        report["images"] = {label:json.loads(run("docker", "image", "inspect", image))[0]["Id"]
                            for label, image in [("engine", image_engine), ("dashboard", image_ui)]}
        report["status"] = "pass"
    except Exception as error:
        report["status"] = "fail"
        report["error"] = str(error)
        for container in created_containers:
            logs = subprocess.run(["docker", "logs", "--tail", "60", container], capture_output=True, text=True, check=False)
            print(logs.stdout, logs.stderr, flush=True)
        raise
    finally:
        cleanup = []
        for container in reversed(created_containers):
            cleanup.append(subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False).returncode == 0)
        if volume_created:
            cleanup.append(subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False).returncode == 0)
        for image in created_images:
            cleanup.append(subprocess.run(["docker", "image", "rm", image], capture_output=True, check=False).returncode == 0)
        report["cleanupPassed"] = all(cleanup)
        report["completedAt"] = datetime.now(timezone.utc).isoformat()
        fixture_directory.cleanup()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as output:
            output.write(json.dumps(report, indent=2) + "\n")
    if not report["cleanupPassed"]:
        raise RuntimeError("Disposable Docker resources were not completely removed")
    print(json.dumps({"status":report["status"], "checks":len(report["checks"]), "output":str(args.output)}))


if __name__ == "__main__":
    main()
