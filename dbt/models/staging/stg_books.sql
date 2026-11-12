select
    record_key as book_id,
    url as book_url,
    title,
    price_gbp,
    case rating when 'One' then 1 when 'Two' then 2 when 'Three' then 3 when 'Four' then 4 when 'Five' then 5 end
        as rating_stars,
    coalesce(availability ilike '%in stock%', false) as in_stock,
    cast(first_seen_at as timestamptz) as first_seen_at,
    cast(last_seen_at as timestamptz) as last_seen_at,
    times_seen,
    content_versions
from {{ source('silver', 'books_toscrape') }}
