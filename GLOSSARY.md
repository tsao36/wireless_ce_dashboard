# Wireless CE Issue Dashboard

This context tracks customer issues, their promotion into Jira, and turnaround across the support workflow.

## Language

**Promoted & Closed Customer Bug**:
A customer-found issue with a positive IPS case number, an IPS promotion status of Promoted or Done, and a computed closed status.
_Avoid_: Promoted bug, closed bug (when used without the full population criteria)

**Closed & Never Promoted IPS**:
A positive IPS case with a closed IPS status and a promotion status other than Promoted or Done.
_Avoid_: Closed unpromoted issue (when the IPS-only population is intended)

**CFE Turnaround Time (TAT)**:
The whole-day average of IPS-to-Jira, Jira sighting, and Jira pending hours; it excludes Jira open and in-progress hours.
_Avoid_: Total TAT (when referring specifically to the CFE measure)

**CFE Team**:
The dashboard's team classification for an issue: WiFi CFE, BT CFE, CIE CFE, or Other.
_Avoid_: Team (when the CFE classification is intended)