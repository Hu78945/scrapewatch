-- The Playwright-rendered /js/ pages must yield exactly the quotes the static pages do. A row here means one
-- fetcher saw a quote the other did not: a rendering, wait-for or pagination bug in the browser path.
with http as (select quote_text, author from {{ ref('stg_quotes') }} where fetcher = 'http'),
     browser as (select quote_text, author from {{ ref('stg_quotes') }} where fetcher = 'browser')
select 'missing_in_browser' as side, * from (select * from http except select * from browser)
union all
select 'missing_in_http' as side, * from (select * from browser except select * from http)
