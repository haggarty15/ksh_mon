# ksh_mon — Vanguard System Monitor

A purpose-built, self-hosted dashboard for monitoring Riot Vanguard (anti-cheat) activity on a Windows 10 machine. Lightweight, local-only, and highly configurable.

---

## What it monitors

| Event type | Description |
|------------|-------------|
| **driver** | Vanguard kernel driver (`vgk.sys` / `vgc.exe`) load/unload events from Windows Event Log |
| **network** | Active TCP connections made by `vgc.exe` / `RiotClientServices.exe` — IPs and ports |
| **error** | System errors and crashes (BSODs, bugchecks) attributed to vgk or vgc |
| **shell_ext** | SecuROM shell extension (`cmdlineext_x64.dll`) registration status — known Explorer crash culprit |
| **crash** | Explorer.exe crashes (0xc0000005 access violations and Windows Error Reporting entries) |

---

## Stack

| Layer | Technology |
|-------|-----------|
| Collector | PowerShell 5.1+ script (`collector/collect.ps1`) |
| Backend | Python 3.13 + [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) |
| Storage | SQLite (via `aiosqlite`) |
| Frontend | Single-page HTML/JS dashboard — no build step required |
| Scheduler | Windows Task Scheduler (daily 3 AM + on-logon trigger) |

---

## Prerequisites

- Windows 10 with PowerShell 5.1 or later (PowerShell 7 also works)
- Python 3.9+ (tested on 3.13)
- Administrator rights for Task Scheduler registration and Event Log access

---

## Quick Start

### 1. Clone the repo

```powershell
git clone https://github.com/haggarty15/ksh_mon.git
cd ksh_mon
```

### 2. Run setup (Administrator PowerShell)

```powershell
.\setup.ps1
```

This will:
- Create a Python virtual environment in `.venv/`
- Install backend dependencies
- Initialise the SQLite database (`data/vanguard_monitor.db`)
- Register the Windows Task Scheduler task `VanguardMonitor\DailyCollect`

### 3. Start the server

```powershell
.venv\Scripts\python -m uvicorn backend.main:app --reload
```

### 4. Open the dashboard

Navigate to **http://127.0.0.1:8000** in your browser.

### 5. Run the collector manually

```powershell
powershell -File collector\collect.ps1 -ApiBaseUrl http://127.0.0.1:8000
```

Or click the **▶ Run Now** button in the dashboard.

---

## Configuration

Edit `config.json` to customise behaviour:

```jsonc
{
  "db_path": "data/vanguard_monitor.db",  // path to SQLite database
  "api_key": "",                           // set to require x-api-key header on trigger/summary endpoints
  "collector": {
    "event_log_hours_back": 24,           // how far back to scan Event Log
    "processes_to_monitor": [             // processes whose connections are tracked
      "vgc.exe", "RiotClientServices.exe", "vgk.sys"
    ],
    "driver_names": ["vgk", "vgc"],       // strings to match in Event Log messages
    "shell_ext_dll": "cmdlineext_x64.dll",// SecuROM DLL name to check
    "output_json_path": "data/latest_collection.json"
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8000
  },
  "anomaly": {
    "new_ip_threshold": 1,               // flag if ≥ N new IPs appear today
    "connection_count_threshold": 50,     // flag if total connections exceed N
    "alert_on_crash": true,              // flag any crash event
    "baseline_days_back": 7              // how many past days form the IP baseline
  },
  "google_home": {
    "device_ip": "",                     // IP of Google Home / Nest (leave empty to disable TTS)
    "tts_language": "en"                 // BCP-47 language tag
  }
}
```

---

## Project structure

```
ksh_mon/
├── collector/
│   └── collect.ps1               # PowerShell data collector
├── backend/
│   ├── __init__.py
│   ├── main.py                   # FastAPI app + API routes
│   ├── database.py               # Async SQLite helpers
│   ├── models.py                 # Pydantic models
│   ├── anomaly.py                # Anomaly detection logic
│   ├── tts.py                    # Google Home TTS (pychromecast)
│   ├── requirements.txt
│   └── tests/
│       ├── __init__.py
│       └── test_api.py           # Pytest tests
├── frontend/
│   └── index.html                # Single-page dashboard
├── scheduler/
│   └── vanguard_monitor_task.xml # Windows Task Scheduler XML
├── config.json                   # Configuration
├── setup.ps1                     # One-time setup script
├── pytest.ini
└── .gitignore
```

---

## API reference

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/api/ingest` | Receive events from the PowerShell collector | No |
| `GET` | `/api/events` | Query stored events (`event_type`, `date`, `limit`, `offset`) | No |
| `GET` | `/api/dates` | List dates that have events | No |
| `GET` | `/api/counts` | Event counts per type (optional `date` filter) | No |
| `POST` | `/api/trigger` | Run collector on-demand (local mode) or queue a trigger for the Windows collector to pick up (cloud mode — `CLOUD_MODE=1`) | If key set |
| `GET` | `/api/trigger/pending` | Check and consume the pending-trigger flag (used by Windows collector in poll mode) | If key set |
| `GET` | `/api/summary/latest` | Last 24 h digest as plain text (for TTS / IFTTT) | If key set |

Interactive docs: **http://127.0.0.1:8000/docs**

---

## Running tests

```powershell
.venv\Scripts\pip install pytest pytest-asyncio httpx
.venv\Scripts\pytest
```

---

## Task Scheduler — manual registration

If you prefer to register the task manually:

```powershell
# Replace REPO_ROOT_PLACEHOLDER with the actual path first, then:
schtasks /Create /TN "VanguardMonitor\DailyCollect" /XML scheduler\vanguard_monitor_task.xml /F
```

To trigger on-demand from the command line:

```powershell
schtasks /Run /TN "VanguardMonitor\DailyCollect"
```

---

## Notes

- **Local-only mode** — leave `api_key` empty in `config.json` and no auth is required. Do not expose the server to the network without setting a key.
- The collector requires elevated privileges to read the Windows Security/System Event Log.
- Sysmon events (if Sysmon is installed) will be captured automatically via the System log queries.
- The SecuROM `cmdlineext_x64.dll` monitoring tracks the Explorer crash root cause identified in May 2025.

---

## Google Home integration

### Architecture overview

```
Existing app (FastAPI + SQLite)
    ↓
Cloudflare Tunnel (free, always-on Windows service)
    ↓ stable public HTTPS endpoint
┌─────────────────────────────────┐    ┌──────────────────────────────────┐
│  INBOUND — voice → app          │    │  OUTBOUND — app → voice          │
│  "Hey Google, run report"       │    │  App detects anomaly             │
│      ↓                          │    │      ↓                           │
│  IFTTT Webhooks                 │    │  pychromecast TTS cast           │
│      ↓                          │    │      ↓                           │
│  POST /api/trigger              │    │  Google Home speaks summary      │
└─────────────────────────────────┘    └──────────────────────────────────┘
```

### 1 — Set an API key

Edit `config.json` and fill in `api_key`:

```json
{
  "api_key": "your-long-random-secret",
  ...
}
```

All requests to `/api/trigger` and `/api/summary/latest` must include:

```
x-api-key: your-long-random-secret
```

Alternatively set the `VANGUARD_API_KEY` environment variable (useful in containers).

### 2 — Configure anomaly thresholds and Google Home

```jsonc
{
  "anomaly": {
    "new_ip_threshold": 1,           // flag if ≥ N new IPs appear today
    "connection_count_threshold": 50, // flag if total connections exceed N
    "alert_on_crash": true,           // flag any crash event
    "baseline_days_back": 7           // how many past days form the IP baseline
  },
  "google_home": {
    "device_ip": "192.168.1.x",      // IP of your Google Home / Nest device
    "tts_language": "en",            // BCP-47 language tag
    "announce_summary_on_trigger": true // speak "run complete" summary when no anomaly is found
  }
}
```

Leave `device_ip` empty to disable TTS (anomaly detection still runs).

### 3 — Cloudflare Tunnel (inbound from internet)

Cloudflare Tunnel exposes your local server at a stable public HTTPS URL with
no port-forwarding or static IP required.

```powershell
# 1. Download cloudflared
winget install Cloudflare.cloudflared          # or download from https://github.com/cloudflare/cloudflared/releases

# 2. Authenticate (opens browser — one-time)
cloudflared tunnel login

# 3. Create a named tunnel
cloudflared tunnel create vanguard-monitor

# 4. Create the config file  C:\Users\YOU\.cloudflared\config.yml
#    (replace <TUNNEL-ID> with the UUID printed in step 3)
tunnel: <TUNNEL-ID>
credentials-file: C:\Users\YOU\.cloudflared\<TUNNEL-ID>.json
ingress:
  - hostname: vanguard.yourdomain.com
    service: http://127.0.0.1:8000
  - service: http_status:404

# 5. Install as a Windows service (runs at boot, no login required)
cloudflared service install

# 6. Start
net start cloudflared
```

Your app is now reachable at `https://vanguard.yourdomain.com`.

> **No code changes to the app are needed** — Cloudflare Tunnel forwards HTTPS
> traffic straight to the local FastAPI server.

### 4 — IFTTT voice trigger (inbound)

1. Create a new **Applet** in IFTTT.
2. **If**: Google Assistant → "Say a specific phrase" → `run Vanguard report`
3. **Then**: Webhooks → Make a web request
   - URL: `https://vanguard.yourdomain.com/api/trigger`
   - Method: `POST`
   - Content Type: `application/json`
   - Body: `{}`
   - Headers: `x-api-key: your-long-random-secret`

**Cloud mode behaviour:** when the server is hosted on a Linux cloud machine
(i.e. `CLOUD_MODE=1`), it cannot run PowerShell locally.  Instead of failing,
`POST /api/trigger` **queues** the trigger in the database and returns
`{"status": "queued", ...}`.  Your Windows machine picks it up within ~30 s
(see step 5 below).  The IFTTT applet configuration is identical either way.

### 5 — Windows collector poll mode (required for cloud hosting)

When your FastAPI server is running in the cloud rather than locally on Windows,
start a long-lived poll loop on your Windows machine so it can receive remote
triggers:

```powershell
# Run once at login (or register as a Task Scheduler task)
powershell -File collector\collect.ps1 `
    -ApiBaseUrl https://vanguard.yourdomain.com `
    -ApiKey     your-long-random-secret `
    -PollMode
```

The collector checks `GET /api/trigger/pending` every 30 seconds.  When a
voice trigger arrives (step 4), the flag is set; on the next poll the collector
runs a full collection, POSTs the results to `/api/ingest`, and the server
performs anomaly detection and speaks the result on your Google Home.

**To register as a Task Scheduler task** (starts automatically at logon):

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NonInteractive -NoProfile -File `"$PWD\collector\collect.ps1`" -ApiBaseUrl https://vanguard.yourdomain.com -ApiKey your-long-random-secret -PollMode"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "VanguardMonitor\PollMode" -Action $action -Trigger $trigger -RunLevel Highest -Force
```

### 6 — Dashboard / TTS outbound summary

`GET /api/summary/latest` returns a JSON body with a `plain_text` field:

```
"Vanguard report: 14 network connections, 2 new IPs not seen before, no crashes detected"
```

This endpoint can be polled by Home Assistant, Node-RED, or any automation
platform to drive outbound TTS on a schedule.
