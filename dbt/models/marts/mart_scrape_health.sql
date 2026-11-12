-- Daily scrape health per site, from the monitor's run-history log.
select
    site,
    run_date,
    count(*) as runs,
    sum((severity = 'critical')::int) as critical_runs,
    sum((severity = 'warning')::int) as warning_runs,
    round(avg(items), 1) as avg_items,
    round(avg(duration_s), 2) as avg_duration_s,
    sum(requests) as requests,
    sum(retries) as retries,
    arg_max(severity, started_at) as last_severity
from {{ ref('stg_runs') }}
group by site, run_date
