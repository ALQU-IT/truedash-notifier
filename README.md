# TrueDash Notifier

A lightweight Docker container that runs on your TrueNAS SCALE server and delivers reliable push notifications to the TrueDash iOS app via APNs — even when your iPhone hasn't opened the app in days.

## What it does

Polls your TrueNAS server every 15 minutes and sends a push notification when:

- **Pool space** drops below 20% free (clears above 25%)
- **Pool health** changes from ONLINE (degraded, faulted, etc.)
- **App updates** become available (notifies when the count increases)

## How it works

```
TrueNAS REST API → TrueDash Notifier → APNs → iPhone
```

The container runs on TrueNAS itself, polls the local API, and pushes directly to Apple's notification servers. No third-party relay, no cloud backend.

## Setup

### 1. APNs Auth Key

The TrueDash iOS app handles this automatically during install. It provides:

- An APNs `.p8` private key (from the ALQU-IT Apple Developer account)
- Your device push token
- Your TrueNAS connection details

These are stored in `/data/config.json` inside the container (mounted as a persistent volume).

### 2. Install via TrueDash

In the TrueDash app:

1. Go to **Settings → Notifications**
2. Tap **Install Notifier on TrueNAS**
3. The app deploys the container and registers your device automatically

Or manually via **Settings → Notifications → Install Manually** if the automatic install isn't available for your TrueNAS version.

### 3. Manual Docker install (advanced)

```bash
docker run -d \
  --name truedash-notifier \
  --restart unless-stopped \
  -p 7842:7842 \
  -v truedash-notifier-data:/data \
  ghcr.io/alqu-it/truedash-notifier:latest
```

Then register from the TrueDash app or via the API directly:

```bash
curl -X POST http://<truenas-ip>:7842/api/register \
  -H "Content-Type: application/json" \
  -d '{
    "device_token": "<your-apns-device-token>",
    "apns_key_id": "<key-id>",
    "apns_team_id": "<team-id>",
    "apns_key_pem": "-----BEGIN PRIVATE KEY-----\n...",
    "truenas_host": "192.168.1.x",
    "truenas_port": 443,
    "truenas_api_key": "<your-api-key>"
  }'
```

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/register` | Register device + TrueNAS credentials |
| `GET` | `/api/status` | Registration status, last check time |
| `DELETE` | `/api/unregister` | Remove registration and credentials |
| `GET` | `/health` | Container health check |

## Configuration

| Field | Default | Description |
|---|---|---|
| `poll_interval` | `900` | Seconds between checks (min 60) |
| `verify_tls` | `false` | Verify TrueNAS TLS certificate (disable for self-signed) |

## Requirements

- TrueNAS SCALE (any recent version)
- Outbound internet access from TrueNAS to `api.push.apple.com:443`
- TrueDash iOS app v3.0+

## Security

- Credentials are stored in a Docker volume (`/data/config.json`) on your own server
- The APNs key never leaves your network — pushes go directly from TrueNAS to Apple
- The container has no inbound internet exposure; it only needs outbound access to APNs

## License

© 2026 ALQU-IT · Switzerland · All rights reserved
