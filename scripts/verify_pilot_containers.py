"""Build and exercise disposable paper/UI images without external runtime networking.

Requires Docker. Does not use deployment secrets or a production volume. The engine
fixture runs protection/telemetry only, with synthetic ticks and no provider calls.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
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
    try:
        compose_env = {**os.environ, "ANTHROPIC_API_KEY":"synthetic", "ZERODHA_API_KEY":"synthetic",
                       "ZERODHA_ACCESS_TOKEN":"synthetic", "PRAMANA_ZERODHA_TOKENS_JSON":"[1]",
                       "PRAMANA_ZERODHA_SYMBOLS_JSON":'{"1":"INFY"}',
                       "PRAMANA_DASHBOARD_SECRET":"synthetic-container-smoke-session-key-only"}
        rendered = subprocess.run(["docker", "compose", "--env-file", str(ROOT / ".env.example"),
                                   "-f", str(ROOT / "deploy/docker-compose.yml"), "config", "--format", "json"],
                                  env=compose_env, capture_output=True, text=True, check=True, timeout=30)
        services = json.loads(rendered.stdout)["services"]
        for service in services.values():
            assert service["environment"]["TRADING_LIVE_MONEY_ACTIVE"] == "false"
            assert service["environment"]["PRAMANA_LEDGER_PATH"] == "/data/pramana.db"
        assert services["dashboard"]["ports"][0]["host_ip"] == "127.0.0.1"
        assert services["pramana-ghost"]["healthcheck"]["test"][3] == "health"
        report["checks"].append({"composeConfiguration":"pass", "liveEnabled":False, "dashboardBind":"loopback"})
        for dockerfile, image in [("Dockerfile", image_engine), ("Dockerfile.ui", image_ui)]:
            print(f"Building {dockerfile}", flush=True)
            subprocess.run(["docker", "build", "--label", f"org.opencontainers.image.revision={revision}",
                            "-f", str(ROOT / "deploy" / dockerfile), "-t", image, str(ROOT)], check=True, timeout=1200)
            created_images.append(image)
        run("docker", "volume", "create", volume)
        volume_created = True
        shared = ["--network", "none", "--mount", f"type=volume,src={volume},dst=/data",
                  "--mount", f"type=bind,src={ROOT / 'tests'},dst=/qa,readonly",
                  "-e", "TRADING_LIVE_MONEY_ACTIVE=false", "-e", "PRAMANA_TENANT_ID=pilot",
                  "-e", "PRAMANA_LEDGER_PATH=/data/ledger.db", "-e", "PRAMANA_PROOF_DIR=/data/proofs",
                  "-e", "PRAMANA_HALT_FILE=/data/HALT", "-e", "PRAMANA_MARKET_SNAPSHOT=/data/market.json",
                  "-e", f"PRAMANA_RELEASE_REVISION={revision}"]
        run("docker", "run", "-d", "--name", engine, *shared, "--entrypoint", "python", image_engine,
            "/qa/container_runtime_fixture.py", "--state", "/data")
        created_containers.append(engine)
        health_command = ["docker", "exec", engine, "python", "/app/scripts/pilot_ops.py", "health",
                          "--database", "/data/ledger.db", "--tenant", "pilot"]

        def health(expected=0):
            return json.loads(run(*health_command, expected=expected))

        report["checks"].append({"initialEngineHealth":wait_for(health, "engine heartbeat")})
        sdk = run("docker", "exec", engine, "python", "-c",
                  "import kiteconnect, ib_async, certifi; from importlib.metadata import version; "
                  "import json; print(json.dumps({p:version(p) for p in ['kiteconnect','ib_async','certifi']}))")
        report["pilotDependencies"] = json.loads(sdk)
        run("docker", "run", "-d", "--name", ui, *shared,
            "-e", "PRAMANA_CONSOLE_DB=/data/console.sqlite", "-e", "PRAMANA_PUBLIC_ORIGIN=http://localhost:3000",
            "-e", "PRAMANA_DASHBOARD_SECRET=synthetic-container-smoke-session-key-only",
            "-e", "ANTHROPIC_API_KEY=", image_ui)
        created_containers.append(ui)

        def ready():
            return run("docker", "exec", ui, "node", "-e",
                       "fetch('http://localhost:3000/login').then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))")

        def dashboard(phase):
            result = run("docker", "exec", "-e", f"SMOKE_PHASE={phase}", ui,
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
        report["checks"].append({"haltAfterEngineRestart":wait_for(halt_health, "halt after restart")})
        run("docker", "exec", engine, "pramana", "resume")
        wait_for(health, "operator-file halt recovery")
        dashboard("recovered")
        run("docker", "restart", ui)
        wait_for(ready, "dashboard restart")
        dashboard("restarted")
        # Freeze fixture publication, then age the persisted observation explicitly.
        run("docker", "exec", engine, "python", "-c",
            "from pathlib import Path; Path('/data/fixture-mode').write_text('freeze')")

        def frozen():
            return run("docker", "exec", engine, "python", "-c",
                       "from pathlib import Path; assert Path('/data/fixture-observed-mode').read_text()=='freeze'")

        wait_for(frozen, "fixture freeze")
        run("docker", "exec", engine, "python", "-c",
            "import sqlite3,json; db=sqlite3.connect('/data/ledger.db'); "
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
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as output:
            output.write(json.dumps(report, indent=2) + "\n")
    if not report["cleanupPassed"]:
        raise RuntimeError("Disposable Docker resources were not completely removed")
    print(json.dumps({"status":report["status"], "checks":len(report["checks"]), "output":str(args.output)}))


if __name__ == "__main__":
    main()
