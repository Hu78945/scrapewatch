select
    run_id,
    site,
    cast(started_at as timestamptz) as started_at,
    cast(cast(started_at as timestamptz) as date) as run_date,
    duration_s,
    pages,
    items,
    requests,
    retries,
    severity,
    issue_classes
from {{ source('ops', 'run_history') }}
