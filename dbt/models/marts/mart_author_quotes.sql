select
    author,
    count(distinct q.quote_id) as n_quotes,
    count(distinct t.tag) as n_distinct_tags
from {{ ref('stg_quotes') }} q
left join lateral (select unnest(q.tags) as tag) t on true
where q.fetcher = 'http'
group by author
