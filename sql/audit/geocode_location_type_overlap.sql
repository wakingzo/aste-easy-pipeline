-- Read-only audit for retiring public.listings.geocode_location_type.
-- No writes.

select
    coalesce(geocode_location_type, '<NULL>') as geocode_location_type,
    coalesce(geocode_precision, '<NULL>') as geocode_precision,
    count(*) as rows
from public.listings
group by 1,2
order by 1,2;
