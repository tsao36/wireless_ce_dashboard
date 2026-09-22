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

### Batch Runners & Schedulers
- `run_offload_reporter_issues_send_email_scheduler.bat`: Windows Task Scheduler wrapper with network/VPN reachability checks.
- `run_offload_csv_watcher.bat`: Local watcher for real-time offload CSV updates.

---

## 🚚 Moving / Deploying to Another Location
This folder is fully **self-contained**. To move it to another machine or folder:
1. Copy the entire `wireless_ce_dashboard` folder to the target location.
2. Ensure `.env` is present in the root of the folder.
3. Run `python -m venv .venv` and `pip install -r requirements.txt` on the target machine (or rely on system `python`/`py`).
4. Execute any `.bat` script directly from the new location.
