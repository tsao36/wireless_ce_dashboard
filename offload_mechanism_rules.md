# Offload Mechanism Rules (Current)

This document explains the active rule set implemented by the Wireless CE team dashboard.

## 1) Purpose

- Use Jonathan Tsao IPS ownership as the offload queue.
- Recommend one IPS reassignment each run from that queue.
- Always pick the receiving engineer with the lowest current issue count.

## 2) Scope and Inputs

- Main script: offload_reporter_issues.py
- Data source table: --table (default from DB_TABLE, fallback ips_jira_bugs)
- History file: --history-file (default offload_reassignment_history.json)
- Receiver cap: --receiver-max-issues (default from OFFLOAD_RECEIVER_MAX_ISSUES, fallback 9)

## 3) Current Load Calculation Rules

Per reporter, current load is computed as:

- num_unpromoted_hsd
- + num_unpromoted_ips
- + num_jira
- - num_stale
- - num_close_pending

Additional rules:

- Only allowed reporters in ALLOWED_REPORTERS are included.
- Reporters in ALWAYS_EXCLUDED_REPORTERS are excluded from receiver candidates.
- Pending offload history is applied as virtual balancing before receiver ranking:
- source reporter count is decremented by 1
- receiving reporter count is incremented by 1

## 4) Source Queue Rules

- Stage 1 trigger: notify overloaded engineer to assign the intended offload issue to Jonathan Tsao in IPS.
- Offload source queue is fixed to Jonathan Tsao ownership.
- Queue aliases supported: jonathan tsao, joanthan tsao.
- Case selection chooses exactly one case per run:
- most recently created IPS case in queue
- case must pass normalization/open-status filters
- case must not already exist in history for the same table

## 5) Receiver Selection Rules

- Receiver candidates must satisfy all conditions:
- not the selected source reporter
- same-group compatible with source (wifi/bt intersection is not empty)
- current adjusted count <= receiver_max_issues

- Candidate ranking is strict ascending by:
- current adjusted count
- reporter name (tie-breaker)

- Rotation is disabled:
- no recent-receiver exclusion
- no inactivity reset logic
- no pending-receiver exclusion

## 6) History State Rules

- History entry default state is pending.
- A pending entry becomes realized when current reporter equals receiving_reporter.
- A pending entry becomes diverted when ownership leaves source but does not match receiving_reporter.
- cancelled entries remain for audit.

## 7) Pending Reminder Rules

For each pending entry, reminder is sent only when all conditions hold with current adjusted counts:

- giving engineer current count > threshold
- giving engineer current count > receiving engineer current count
- receiving engineer current count <= receiver_max_issues

Reminder recipients:

- primary: reporter_email_map for giving/receiving engineers
- fallback: default to-list from recipients file if no mapping resolved

## 8) Email and Persistence Rules

- Recommendation/reminder sending requires --send-email and not --no-email.
- --dry-run prints content but does not send email and does not append new history recommendation.
- Real send path:
- Graph delegated mode when GRAPH_AUTH_MODE=delegated
- Graph app mode otherwise, requiring GRAPH_SENDER_UPN
- New recommendation history entry is appended only after non-dry-run recommendation flow completes.
