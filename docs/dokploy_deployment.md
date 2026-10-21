# Hostinger / Dokploy deployment

This Compose deployment builds React and runs FastAPI with one worker. Nginx
serves the dashboard and forwards /api, /health and /webhooks to the API.
The database remains external. No migrations or data resets run on startup.

## Configure

1. Keep Autodeploy off during initial setup. Select the GitHub repository,
   reviewed branch `feature/linetech-preview` and Compose Path ./docker-compose.yml, then Save. Keep Autodeploy off for manual releases.
2. In Environment, supply the actual server values below. Keep the existing
   pilot PINs if required; do not put them in GitHub or frontend variables.

```dotenv
DATABASE_URL=<your existing remote PostgreSQL connection string>
MANAGEMENT_PIN=<existing management PIN>
ENGINEERING_PIN=<existing engineering PIN>
HMI_DEVICE_PIN=<existing tablet PIN>
PULSE_ORIGIN=https://<your app hostname>
```

These are placeholders, not working credentials. PULSE_ORIGIN has no trailing
slash. A database address of localhost refers to the container, not your PC.
Optional WHATSAPP_VERIFY_TOKEN and WHATSAPP_ALLOWED_GROUP can be supplied if
using the webhook. Session durations default to 30 minutes.

3. Point the chosen hostname's DNS A record at the VPS address.
4. In Domains, add that hostname, service web, container port 80, path /;
   enable HTTPS with Let's Encrypt. Dokploy supplies the Traefik routing.
   Do not publish the API's port 8000 or add a separate API domain.
5. Confirm the target database schema against docs/deployment_release_gate.md.
   Take and verify the required backup before any separate migration work.
6. Deploy and inspect both build logs and container status.

## Verify

- / opens the app; /dashboard and /engineering also work after refresh.
- /health returns API JSON, not the frontend HTML.
- /health/ready returns HTTP 200; 503 means database/schema is not ready.
- Tablet, engineering and management login work with the configured PINs.
- Complete the physical-device release checks before operational use.

Keep one API container / worker: authentication sessions and rate limits are
in memory. A restart signs users out. The web health check checks static serving;
the API readiness check separately checks the database and schema. No browser observer process is started by this deployment. The API image installs Chromium for on-demand report PDF/PNG exports. Verify both formats after deployment.

For updates, deploy a reviewed commit and retain the previous known-good commit
for rollback. Do not use Fresh Volumes as a deployment or troubleshooting step.

Local validation when Docker is available:

```sh
docker compose config --quiet
docker compose build
```

Required environment values must be supplied even for Compose validation. Never
paste rendered Compose output publicly because it includes environment secrets.

Dokploy domain instructions: https://docs.dokploy.com/docs/core/docker-compose/domains
