-- Read-only verification after migration 009.
select
  coalesce(geocode_precision,'<NULL>') as geocode_precision,
  count(*) as rows
from public.listings
group by 1
order by 1;
