"""Research commands only; never submits broker orders.

Run with PYTHONPATH=src from the repository. Credential values are never arguments.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from quant_ai.research.company_events import CompanyEvents
from quant_ai.research.lab import ResearchLab, instant
from quant_ai.research.portfolio_sim import PortfolioJournal
from quant_ai.research.providers import Provider, evaluate_case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "compare",
            "simulate",
            "event-fetch",
            "event-import",
            "event-map",
            "event-sources",
            "event-status",
        ),
    )
    parser.add_argument("database", type=Path)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--experiment")
    parser.add_argument("--case-id")
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--symbol")
    parser.add_argument("--at")
    parser.add_argument("--decisions-from", type=Path)
    args = parser.parse_args()
    if args.operation == "compare":
        if not all((args.experiment, args.case_id, args.receipts)):
            parser.error("compare requires --experiment, --case-id and --receipts")
        lab = ResearchLab(args.database)
        try:
            config = lab.config(args.experiment)
            providers = {}
            for candidate, settings in config["providers"].items():
                provider = settings["provider"]
                if provider not in ("openai", "anthropic"):
                    raise ValueError("unsupported_provider")
                key = os.environ.get(
                    "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
                )
                path = Path.home() / ".config" / "pramana" / (provider + ".key")
                if not key and path.exists():
                    key = path.read_text().strip()
                providers[candidate] = Provider(provider, settings["model"], key)
            result = evaluate_case(lab, args.experiment, args.case_id, providers, args.receipts)
            print(
                json.dumps({"decisions": result, "report": lab.report(args.experiment)}, indent=2)
            )
        finally:
            lab.close()
    elif args.operation == "simulate":
        if args.file is None:
            parser.error("simulate requires --file containing config and events")
        data = json.loads(args.file.read_text())
        events = data["events"]
        if args.decisions_from:
            if not args.experiment:
                parser.error("--decisions-from requires --experiment")
            lab = ResearchLab(args.decisions_from, readonly=True)
            try:
                evidence = lab.export_evidence(args.experiment)["body"]
            finally:
                lab.close()
            for case in evidence["cases"]:
                for candidate, decision in case["decisions"].items():
                    if decision["status"] == "ok" and decision["action"] == "BUY":
                        events.append(
                            {
                                "id": "decision:"
                                + args.experiment
                                + ":"
                                + case["case_id"]
                                + ":"
                                + candidate,
                                "kind": "order",
                                "at": decision["decided_at"],
                                "candidate": candidate,
                                "symbol": case["packet"]["exchange"]
                                + ":"
                                + case["packet"]["symbol"],
                                "side": "BUY",
                                "quantity": decision["quantity"],
                                "decision_ref": case["input_digest"],
                            }
                        )
            events.sort(key=lambda event: instant(event["at"]))
        journal = PortfolioJournal(args.database, data["config"])
        try:
            for event in events:
                journal.append(event)
            print(json.dumps(journal.report(), indent=2))
        finally:
            journal.close()
    else:
        store = CompanyEvents(args.database)
        try:
            if args.operation == "event-fetch":
                result = store.fetch()
            elif args.operation == "event-import":
                if args.file is None or not args.at:
                    parser.error("event-import requires --file XML and --at capture timestamp")
                result = store.ingest(args.file.read_bytes(), observed_at=args.at)
            elif args.operation == "event-map":
                if args.file is None:
                    parser.error("event-map requires --file JSON mapping")
                store.map_company(**json.loads(args.file.read_text()))
                result = store.status()
            elif args.operation == "event-sources":
                if not args.symbol or not args.at:
                    parser.error(
                        "event-sources requires --symbol NSE:TICKER and --at decision timestamp"
                    )
                result = store.sources_as_of(args.symbol, args.at)
            else:
                result = store.status()
            print(json.dumps(result, indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
