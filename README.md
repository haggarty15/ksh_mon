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
  "db_path": "data/vanguard_monitor.db",        // path to SQLite database
  "collector": {
    "event_log_hours_back": 24,                 // how far back to scan Event Log
    "processes_to_monitor": [                   // processes whose connections are tracked
      "vgc.exe", "RiotClientServices.exe", "vgk.sys"
    ],
    "driver_names": ["vgk", "vgc"],             // strings to match in Event Log messages
    "shell_ext_dll": "cmdlineext_x64.dll",      // SecuROM DLL name to check
    "output_json_path": "data/latest_collection.json"
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8000
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

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/ingest` | Receive events from the PowerShell collector |
| `GET` | `/api/events` | Query stored events (`event_type`, `date`, `limit`, `offset`) |
| `GET` | `/api/dates` | List dates that have events |
| `GET` | `/api/counts` | Event counts per type (optional `date` filter) |
| `POST` | `/api/trigger` | Trigger the PowerShell collector on-demand |

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

- **No authentication** — designed for local-only use. Do not expose the server to the network.
- The collector requires elevated privileges to read the Windows Security/System Event Log.
- Sysmon events (if Sysmon is installed) will be captured automatically via the System log queries.
- The SecuROM `cmdlineext_x64.dll` monitoring tracks the Explorer crash root cause identified in May 2025.
