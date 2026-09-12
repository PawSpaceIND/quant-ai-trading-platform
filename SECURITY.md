# Security Policy

## Secrets
- Never commit broker, market-data, news, AI or database credentials.
- Use environment/secret-manager injection only.
- Keep paper and live credentials physically separate.
- Rotate production credentials and revoke immediately on suspected exposure.

## Live trading safety
- Live order submission is disabled by default.
- `TRADING_MODE=LIVE` alone is insufficient; explicit live permission is also required.
- Risk firewall, kill switch, idempotency and audit journaling are mandatory controls.
- No prediction/model component may bypass deterministic risk approval.

## Tenant isolation
- Tenant-scoped credentials and positions must never cross tenant boundaries.
- Commercial API access requires authentication, rate limiting and usage metering.
