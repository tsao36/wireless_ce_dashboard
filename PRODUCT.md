# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Not confirmed by the product owner. Repository evidence suggests Wireless CE engineers whose issue loads are tracked and operators who run the loading and offload workflow. Treat these as working inferences, not approved personas.

## Product Purpose

The README describes this project as an independent Wireless CE team loading dashboard and offload management system for IPS customer cases and issue tracking. The dashboard presents weighted team loading and daily snapshots; the automation prepares offload recommendations and related email reminders. The intended user outcome and success measure have not been confirmed.

## Positioning

Not confirmed by the product owner. The implemented mechanism combines issue-load calculations, category weights, eligibility rules, and reassignment history to select an eligible low-load receiver for an IPS offload recommendation. Do not present this implementation as a verified market or organizational differentiator.

## Operating Context

- The project is a Windows-oriented Python application with a browser-based dashboard and batch-script workflows.
- The dashboard reads local JSON snapshots and history, and exposes a local HTTP API; its documented default port is 8010.
- The README requires database and Microsoft Graph credentials in `.env` for the automated workflows.
- Daily loading summaries and offload recommendations can be run through batch files; email sending is an explicit execution option.

## Capabilities and Constraints

- The dashboard supports date selection, member search, category filtering, sorting, weighted-load details, and running the daily loading batch.
- The repository describes load inputs from IPS, Jira, and HSD, with configurable category and technology weights.
- The offload implementation recommends one eligible IPS case at a time, records recommendation history, and ranks receiver candidates by current adjusted load, then reporter name. The documented default receiver cap is 9 issues.
- The source queue is documented as IPS ownership assigned to Jonathan Tsao. This is an implemented workflow constraint; whether it remains an approved product policy is unconfirmed.
- Offload policy conflict needs owner confirmation: `offload_mechanism_one_pager.md` describes a group-scoped 10-day recent-receiver block and inactivity reset, while `offload_mechanism_rules.md` says rotation is disabled. The current selection code logs that it chooses the lowest-count candidate without rotation. Do not silently change or claim the disputed policy.
- Product-specific accessibility requirements and the intended success metric have not been established.

## Evidence on Hand

- `README.md` describes the product scope, setup, and automation workflows.
- `index.html` and `loading_dashboard.py` implement the weighted team loading dashboard and its local API.
- `offload_reporter_issues.py` implements offload selection and notification behavior.
- `issue_category_weights.json`, `team_loading_latest.json`, `loading_history/`, and `offload_reassignment_history.json` provide configuration or workflow data. Treat live data as operational data, not as fabricated demonstration content.

## Product Principles

Not confirmed. The implementation has auditable calculations and history, but these are repository-derived characteristics rather than product-owner-approved principles.