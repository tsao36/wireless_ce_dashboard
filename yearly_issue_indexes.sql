-- Run outside an explicit transaction. CONCURRENTLY keeps reads available.
-- Re-run after ips_jira_bugs_v2 is replaced by its external refresh process.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ips_jira_bugs_year_issue_date_idx
ON ips_jira_bugs (
    (CASE
        WHEN TRIM(COALESCE(ips_case_number::text, '')) ~ '^[0-9]*[1-9][0-9]*$'
            THEN COALESCE(ips_created_date, jira_created_date)
        WHEN hsd_submitted_date IS NOT NULL THEN hsd_submitted_date
        ELSE jira_created_date
    END)
);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ips_jira_bugs_year_issue_year_idx
ON ips_jira_bugs (
    (EXTRACT(YEAR FROM (
        CASE
            WHEN TRIM(COALESCE(ips_case_number::text, '')) ~ '^[0-9]*[1-9][0-9]*$'
                THEN COALESCE(ips_created_date, jira_created_date)
            WHEN hsd_submitted_date IS NOT NULL THEN hsd_submitted_date
            ELSE jira_created_date
        END
    ))::int)
);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ips_jira_bugs_v2_year_issue_date_idx
ON ips_jira_bugs_v2 (
    (CASE
        WHEN TRIM(COALESCE(ips_case_number::text, '')) ~ '^[0-9]*[1-9][0-9]*$'
            THEN COALESCE(ips_created_date, jira_created_date)
        WHEN hsd_submitted_date IS NOT NULL THEN hsd_submitted_date
        ELSE jira_created_date
    END)
);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ips_jira_bugs_v2_year_issue_year_idx
ON ips_jira_bugs_v2 (
    (EXTRACT(YEAR FROM (
        CASE
            WHEN TRIM(COALESCE(ips_case_number::text, '')) ~ '^[0-9]*[1-9][0-9]*$'
                THEN COALESCE(ips_created_date, jira_created_date)
            WHEN hsd_submitted_date IS NOT NULL THEN hsd_submitted_date
            ELSE jira_created_date
        END
    ))::int)
);

ANALYZE ips_jira_bugs;
ANALYZE ips_jira_bugs_v2;