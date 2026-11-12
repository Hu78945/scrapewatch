-- quotes_toscrape (static HTML, httpx) and quotes_toscrape_js (client-rendered, Playwright) are the same corpus
-- served two ways; both are kept so the parity test can prove the browser path extracts the same data.
{% for site, fetcher in [('quotes_toscrape', 'http'), ('quotes_toscrape_js', 'browser')] %}
select
    record_key as quote_id,
    '{{ fetcher }}' as fetcher,
    text as quote_text,
    author,
    coalesce(tags, []) as tags,
    cast(last_seen_at as timestamptz) as last_seen_at
from {{ source('silver', site) }}
{% if not loop.last %}union all{% endif %}
{% endfor %}
