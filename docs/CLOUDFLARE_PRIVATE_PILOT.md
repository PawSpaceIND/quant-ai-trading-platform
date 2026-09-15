# Private interactive pilot through Cloudflare

This deployment serves the complete existing dashboard, including watchlist editing and Atlas AI, from the persistent Docker host through Cloudflare. The separate Worker/D1 snapshot deployment remains read-only. This file prepares deployment; it is not evidence that an account, host or pilot is qualified.

## Configure before starting

1. Choose an always-on Docker host with persistent storage and complete the private pilot runbook. A development computer is sufficient for attended testing only. Retain paper-only execution.
2. Choose a hostname in your Cloudflare account. Create a self-hosted Cloudflare Access application for that entire hostname, with an allow policy restricted to named pilot users. Do this before publishing the tunnel route. Do not add a bypass policy.
3. Create a remotely managed Cloudflare Tunnel. Configure its published application route to `http://dashboard:3000` (the Compose service address, not localhost inside the tunnel container). Apply an explicit cache bypass rule for the hostname, including authenticated pages and APIs.
4. Store the tunnel token in a private file outside the repository, readable by the tunnel container. Set `PRAMANA_TUNNEL_TOKEN_FILE` to its absolute host path in the deployment environment. Never put its value in a command argument, source control or a review artifact. Compose mounts it as a file; this is not an encrypted secret store.
5. Set `PRAMANA_CLOUDFLARED_IMAGE` to a reviewed `cloudflare/cloudflared@sha256:...` image. The selected release must support `--token-file`. Set `PRAMANA_PUBLIC_ORIGIN=https://your-pilot-hostname` and retain the dashboard's own secret and sign-in. Cloudflare Access does not replace application authentication.

From `deploy/`, validate configuration privately, then start:

```sh
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.cloudflare.yml config --quiet
docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.cloudflare.yml up -d --build
```

The overlay publishes no additional host ports and mounts no trading database or broker secrets into the tunnel container. The base dashboard port remains loopback-only. Rotate the tunnel token through Cloudflare if exposed and replace the local secret file before recreating the connector.

## Required deployment evidence

Record the exact revision, image digest, target host and hostname. Verify an unauthenticated browser is intercepted by Access; an approved user still encounters dashboard sign-in; a rejected user cannot access pages or APIs. Verify sign-in, sign-out, a persistent watchlist edit, an Atlas reply, stale-feed state, and the paper halt flow through the actual HTTPS hostname. Check cross-origin mutations are rejected. Restart the host and verify database persistence and connector recovery. Independently test alert delivery and restore/rollback using the runbook. Retain redacted results under X02; do not mark that gate passed merely because the tunnel connects.

The host and authorized account configuration are still required. No tunnel or DNS change is performed by adding this overlay.

References: [Cloudflare self-hosted Access application](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/), [tunnel run parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/).
