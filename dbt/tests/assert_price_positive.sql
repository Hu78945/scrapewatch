select * from {{ ref('stg_books') }} where price_gbp <= 0
