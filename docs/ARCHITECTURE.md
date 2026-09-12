# Architecture

## Core principles
1. Research, prediction, risk, and execution are separate boundaries.
2. Risk decisions are deterministic, inspectable, and cannot be overridden by an AI model.
3. Broker implementations conform to one interface.
4. India and US execution are isolated adapters sharing canonical domain models.
5. Every decision and fill will be journaled for replay and audit.
6. V1 is paper-only and contains no live broker implementation.

## Planned services
- Market data ingestion
- Historical lake and replay engine
- Feature and regime engine
- Strategy ensemble
- Probability calibration
- Portfolio construction
- Risk firewall
- Paper/live execution adapters
- Trade journal and learning loop
- Web dashboard
