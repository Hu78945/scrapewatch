select
    rating_stars,
    count(*) as n_books,
    round(avg(price_gbp), 2) as avg_price_gbp,
    min(price_gbp) as min_price_gbp,
    max(price_gbp) as max_price_gbp,
    round(avg(in_stock::int), 3) as in_stock_share
from {{ ref('stg_books') }}
group by rating_stars
