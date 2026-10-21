# Wireless CE Team Dashboard

An independent Wireless CE team loading dashboard and offload management system for IPS customer cases and issue tracking.

---

## 📁 Project Structure

```
wireless_ce_dashboard/
├── .env                                       # Local environment variables & secrets
├── .gitignore                                 # Git ignore rules
├── requirements.txt                           # Python dependencies
├── README.md                                  # Project documentation
│
├── offload_reporter_issues.py                 # Core offload automation logic & email dispatcher
├── watch_offload_csv_notify.py                # CSV watcher sentinel for offload log changes
├── db_health_notify.py                        # Database health sentinel updater
├── Wireless_bug_dashboard.py                  # Postgres database connector shim (DbConnector)
├── Meeting_agenda_OneNote.py                  # Microsoft Graph token & mail delivery client
├── issue_category_model.py                    # ML issue classifier and row weight analyzer
│
├── recipients.json                            # Default email recipients mapping
├── issue_category_weights.json                # Weight configuration map for categories
├── offload_reassignment_history.json          # Historical record of case reassignments
├── _tmp_offload_history_empty.json            # Template for resetting history
├── offload_mechanism_rules.md                 # Rule specifications for offload logic
├── offload_mechanism_one_pager.md             # One-page executive summary
│
├── APIs/
│   └── Sherlock.py                            # Database configuration loader
├── models/
│   └── issue_category_model.joblib            # Pre-trained ML category classifier model
└── logs/                                      # Execution logs directory
```

---

## 🚀 Quick Start

### 1. Environment Setup
Ensure Python 3.10+ (or Python 3.14) is installed. You can set up a local virtual environment:

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Credentials (.env)
Ensure `.env` contains valid DB and Graph API credentials:
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASS`
- `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_SENDER_UPN`

The Jira Cloud base URL defaults to `https://jira.idoc.intel.com`. For example, the issue `WIFI-335395` opens at `https://jira.idoc.intel.com/browse/WIFI-335395`. Set `JIRA_CLOUD_SERVER` to override the base URL; use only the host/base path, without `/browse/<issue-key>`. The retired Data Center host is not used. Existing connector credentials are still read from `JIRA_USER` and `JIRA_PASSWORD`. Confirm the required Cloud authentication procedure with the internal Jira automation runbook before configuring those credentials.
---

## 🏃 Running the Automation

### Daily Summary Execution
Run `run_offload_loading_summary_daily.bat` to analyze workload and generate daily reports:
```cmd
run_offload_loading_summary_daily.bat
```

### Offload Reporter & Reassignment Engine
Run `run_offload_reporter_issues.bat` directly or with options:
```cmd
run_offload_reporter_issues.bat --send-email
```

### Loading Dashboard Backend (IIS API proxy target)
The IIS site proxies `/api/*` to a local `loading_dashboard.py` server on port 8010
(see `web.config`). This backend does not start on its own — run `run_loading_dashboard.bat`
to launch it (it loops and restarts the server if it crashes). To make it survive a
server reboot, register it as a startup Scheduled Task once (elevated PowerShell):
```powershell
powershell -ExecutionPolicy Bypass -File Register-DashboardStartupTask.ps1
```
This creates a "Wireless CE Dashboard Backend" task that runs `run_loading_dashboard.bat`
at every system boot under SYSTEM.

Note: IIS site bindings (physical path, host headers, certificate) are configured directly
in IIS, not via this repo — if the project folder is ever moved or renamed, the IIS site's
physical path must be updated to match, or the site will fail with a 500.19 error.


### Batch Runners & Schedulers
- `run_offload_reporter_issues_send_email_scheduler.bat`: Windows Task Scheduler wrapper with network/VPN reachability checks.
- `run_offload_csv_watcher.bat`: Local watcher for real-time offload CSV updates.

## Team Activity Dashboard
Run `loading_dashboard.py` from the project root to serve the dashboard and its local APIs:

```cmd
python loading_dashboard.py --host 127.0.0.1 --port 8010
```

The activity view reads `CFE work overview.xlsx` from the project root. Replace that local workbook with the latest SharePoint copy, then select **Refresh workbook** in the page. The view includes current-year data and unversioned operating/reference sheets; worksheets explicitly labeled for other years remain in Excel. Formula results use the values saved by Excel.

To refresh the team-loading snapshot without sending notifications or changing offload history, run `python offload_reporter_issues.py --snapshot-only`. Each run also appends the engineer issue lists to `issue history/YYYY-Www.json` for the corresponding ISO week; the loading page uses the latest snapshot when a member name is opened.

Loading counts, issue details, and category weights use the same current-calendar-year and open-status eligibility rules. An IPS-only row cannot make a linked Jira active when another database record for that Jira is Closed, Verify, or Implemented. Eligible issues are selected by these rules, not by truncating an all-years issue list to match the count.

Read-only PostgreSQL regression tests use in-query fixtures without changing database records. In PowerShell, run `$env:RUN_LOADING_DB_TESTS='1'; python -m unittest discover -s tests -p test_loading_eligibility.py` with the project environment and database connectivity available.

While the dashboard server is running, it starts a snapshot-only refresh at startup and every 10 minutes, even with no browser open. Automatic refreshes share the manual batch lock and skip a tick if a batch is still running. Output is appended to `logs/loading_auto_refresh.log`; launch failures are retried on the next tick. This reads the database without updating it, sending notifications, or changing offload history. Existing browser tabs still need a page refresh to display the newly saved snapshot. Restart the server after upgrading to enable the timer; no separate Scheduled Task is required for the refresh loop.

The source workbook is excluded from Git because it contains internal team data. The Python API binds to loopback for the IIS reverse-proxy setup in `web.config`; the IIS site itself must remain restricted to the intended internal network because the dashboard does not require individual sign-in.

---

## 🚚 Moving / Deploying to Another Location
This folder is fully **self-contained**. To move it to another machine or folder:
1. Copy the entire `wireless_ce_dashboard` folder to the target location.
2. Ensure `.env` is present in the root of the folder.
3. Run `python -m venv .venv` and `pip install -r requirements.txt` on the target machine (or rely on system `python`/`py`).
4. Execute any `.bat` script directly from the new location.
