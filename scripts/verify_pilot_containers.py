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
        directives.update(starting_capital=123456, max_open_positions=3)
        directives_file.write_text(json.dumps(directives))
        directives_file.chmod(0o644)  # Synthetic data read by UID 10001 through a single-file mount.
        research_paths = {"PRAMANA_RESEARCH_LAB_REPORT":"/data/research/comparison-001.json",
                          "PRAMANA_PORTFOLIO_RESEARCH_REPORT":"/data/research/portfolio-001.json",
                          "PRAMANA_COMPANY_EVENTS_DB":"/data/research/events.sqlite"}
        safe_environment = {key:value for key, value in os.environ.items()
                            if not key.startswith(("PRAMANA_", "ANTHROPIC_", "ZERODHA_", "FRED_", "TRADING_"))}
        compose_env = {**safe_environment, "ANTHROPIC_API_KEY":"synthetic", "ZERODHA_API_KEY":"synthetic",
                       "ZERODHA_ACCESS_TOKEN":"synthetic", "PRAMANA_ZERODHA_TOKENS_JSON":"[1]",
                       "PRAMANA_ZERODHA_SYMBOLS_JSON":'{"1":"INFY"}',
                       "PRAMANA_DASHBOARD_SECRET":"synthetic-container-smoke-session-key-only",
                       "PRAMANA_PUBLIC_ORIGIN":"http://localhost:3000", "PRAMANA_RELEASE_REVISION":revision,
                       "PRAMANA_HOLIDAYS_JSON":'{"INDIA":["2026-09-11"]}',
                       "PRAMANA_DIRECTIVES_HOST_FILE":str(directives_file), **research_paths}
        rendered = subprocess.run(["docker", "compose", "--env-file", str(ROOT / ".env.example"),
                                   "-f", str(ROOT / "deploy/docker-compose.yml"), "config", "--format", "json"],
                                  env=compose_env, capture_output=True, text=True, check=True, timeout=30)
        services = json.loads(rendered.stdout)["services"]
        for service in services.values():
            assert service["environment"]["TRADING_LIVE_MONEY_ACTIVE"] == "false"
            assert service["environment"]["PRAMANA_LEDGER_PATH"] == "/data/pramana.db"
        assert services["dashboard"]["ports"][0]["host_ip"] == "127.0.0.1"
        assert services["pramana-ghost"]["healthcheck"]["test"][3] == "health"
        for key, value in research_paths.items():
            assert services["dashboard"]["environment"][key] == value
        assert services["market-monitor"]["environment"]["PRAMANA_HOLIDAYS_JSON"] == services["pramana-ghost"]["environment"]["PRAMANA_HOLIDAYS_JSON"] == compose_env["PRAMANA_HOLIDAYS_JSON"]
        directives_mount = next(v for v in services["pramana-ghost"]["volumes"] if v["target"] == "/app/directives.json")
        assert directives_mount["source"] == str(directives_file) and directives_mount["read_only"]
        report["checks"].append({"composeConfiguration":"pass", "liveEnabled":False, "dashboardBind":"loopback",
                                 "researchPaths":research_paths, "customDirectivesMount":"read_only"})
        for dockerfile, image in [("Dockerfile", image_engine), ("Dockerfile.ui", image_ui)]:
            print(f"Building {dockerfile}", flush=True)
            subprocess.run(["docker", "build", "--label", f"org.opencontainers.image.revision={revision}",
                            "-f", str(ROOT / "deploy" / dockerfile), "-t", image, str(ROOT)], check=True, timeout=1200)
            created_images.append(image)
        run("docker", "volume", "create", volume)
        volume_created = True
        shared = ["--network", "none", "--mount", f"type=volume,src={volume},dst=/data",
                  "--mount", f"type=bind,src={ROOT / 'tests'},dst=/qa,readonly"]

        def environment(service):
            # Exercise the rendered Compose values; suppress paid provider access in the fixture.
            values = {**services[service]["environment"], "ANTHROPIC_API_KEY":""}
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
        sdk = run("docker", "exec", engine, "python", "-c",
                  "import kiteconnect, ib_async, certifi; from importlib.metadata import version; "
                  "import json; print(json.dumps({p:version(p) for p in ['kiteconnect','ib_async','certifi']}))")
        report["pilotDependencies"] = json.loads(sdk)
        run("docker", "run", "-d", "--name", ui, *shared, *environment("dashboard"), image_ui)
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
