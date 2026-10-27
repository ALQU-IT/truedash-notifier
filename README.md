# TrueDash Notifier

A lightweight app for TrueNAS SCALE that delivers push notifications to the TrueDash iOS app — even when your iPhone hasn't opened the app in days.

[Here to the app](https://apps.apple.com/ch/app/truedash/id6778523884)

## What it does

Polls your TrueNAS server every 10 minutes and sends a push notification when:

- **Pool space** drops below 20% free (clears above 25%)
- **Pool health** gets worse (ONLINE to DEGRADED, and again DEGRADED to FAULTED, etc.)
- **App updates** become available (each app that newly has an update)
- **TrueNAS alerts** of level ERROR or worse appear (SMART, scrub, replication, ...)
- **TrueNAS can't be reached** for an hour (API key revoked or expired, address changed): the phones are told once

## How it works

```
TrueNAS middleware (JSON-RPC over WebSocket) → TrueDash Notifier → truedash-relay.alqu.ch → APNs → iPhone
```

The app polls the local TrueNAS API. When an alert is detected, it sends a wake signal to the relay using an opaque `push_id`. The relay resolves the device token internally and forwards the push to Apple — the device token never leaves the relay.

### Privacy architecture

- The iOS app registers directly with the relay and receives a `push_id` (random UUID) and a per-device `push_secret`
- The app gives the Notifier only the `push_id` and `push_secret` — never the raw APNs device token
- Wake calls contain only the `push_id` — no PII passes through the Notifier or its logs
- Each device has its own secret, independently revokable
- Several phones can share one notifier (1.2.0+): each keeps its own registration, a phone that
  re-enrolls replaces only its own entry (recognised by the relay's opaque `device_id`), and
  removing one phone leaves the others working

## Installation

Install and configure TrueDash Notifier directly from the TrueDash iOS app:

1. Go to **Settings → Notifications**
2. Tap **Install Notifier on TrueNAS**
3. The app deploys the container on your TrueNAS server and configures everything automatically

## API

All endpoints require `Authorization: Bearer <notifier_secret>` (each phone has its own). Registering with an `enrollment_token` is authenticated by the relay instead. Once a phone is registered, a *new* phone is accepted only if its TrueNAS API key logs in to the TrueNAS the notifier already watches (1.3.0), and a full notifier (10 phones) answers `409` instead of dropping one. `DELETE /api/unregister` removes only the calling phone and returns `remaining_devices`, so the app removes the container only when no phone is left.

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/register` | Register device + TrueNAS credentials |
| `GET` | `/api/status` | Registration status and last check time |
| `DELETE` | `/api/unregister` | Remove registration and credentials |
| `GET` | `/health` | Health check (no auth) |

## Configuration

| Field | Default | Description |
|---|---|---|
| `poll_interval` | `600` | Seconds between checks (min 60) |
| `verify_tls` | `false` | Verify TrueNAS TLS certificate (TrueNAS ships a self-signed one). Never verified for `host.docker.internal`, which stays on the machine |
| `truenas_username` | none | The API key's username; used for `auth.login_ex` (needed from TrueNAS 27) |

## Requirements

- TrueNAS SCALE (any recent version)
- Outbound internet access from TrueNAS to `truedash-relay.alqu.ch:443`
- TrueDash iOS app

## Security

- Config is stored at `/data/config.json` with `600` permissions — readable only by the container process
- The Notifier never sees or logs the APNs device token — only the opaque `push_id`
- The relay holds device tokens; the Notifier holds only UUIDs
- Each device authenticates to the relay with its own per-device secret

## License

© 2026 ALQU-IT · Switzerland · All rights reserved
