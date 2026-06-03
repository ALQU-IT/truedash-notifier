# TrueDash Notifier

A lightweight Docker container that runs on your TrueNAS SCALE server and delivers push notifications to the TrueDash iOS app — even when your iPhone hasn't opened the app in days.

## What it does

Polls your TrueNAS server every 10 minutes and sends a push notification when:

- **Pool space** drops below 20% free (clears above 25%)
- **Pool health** changes from ONLINE (degraded, faulted, etc.)
- **App updates** become available (notifies when the count increases)

## How it works

```
TrueNAS REST API → TrueDash Notifier → truedash-relay.alqu.ch → APNs → iPhone
```

The container runs on TrueNAS itself and polls the local API. When an alert condition is detected, it sends a wake signal to the TrueDash relay service, which forwards the push notification to Apple's servers and on to your device.

## Setup

### 1. Install via TrueDash

In the TrueDash app:

1. Go to **Settings → Notifications**
2. Tap **Install Notifier on TrueNAS**
3. The app deploys the container and registers your device automatically

Or manually via **Settings → Notifications → Install Manually** if the automatic install isn't available for your TrueNAS version.

### 2. Manual Docker install (advanced)

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
    "relay_url": "https://truedash-relay.alqu.ch",
    "relay_token": "<your-relay-token>",
    "truenas_host": "192.168.1.x",
    "truenas_port": 443,
    "truenas_api_key": "<your-truenas-api-key>"
  }'
```

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/register` | Register device + TrueNAS credentials |
| `GET` | `/api/status` | Registration status and last check time |
| `DELETE` | `/api/unregister` | Remove registration and credentials |
| `GET` | `/health` | Container health check |

## Configuration

| Field | Default | Description |
|---|---|---|
| `poll_interval` | `600` | Seconds between checks (min 60) |
| `verify_tls` | `true` | Verify TrueNAS TLS certificate (set to false for self-signed certs) |

## Requirements

- TrueNAS SCALE (any recent version)
- Outbound internet access from TrueNAS to `truedash-relay.alqu.ch:443`
- TrueDash iOS app

## Security

- Credentials are stored in a Docker volume (`/data/config.json`) on your own server
- The container only needs outbound access to TrueNAS and the relay service — no inbound internet exposure
- The relay service handles APNs delivery; your device token and relay token are the only credentials transmitted

## License

© 2026 ALQU-IT · Switzerland · All rights reserved
