with tags as (
    select quote_id, author, unnest(tags) as tag
    from {{ ref('stg_quotes') }}
    where fetcher = 'http'
)
select
    tag,
    count(distinct quote_id) as n_quotes,
    count(distinct author) as n_authors
from tags
group by tag
