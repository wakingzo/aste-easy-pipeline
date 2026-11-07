-- Read-only samples to classify legacy geocode_location_type values safely.
-- No writes.

with ranked as (
    select
        l.pvp_id,
        l.indirizzo,
        l.comune,
        l.geocode_location_type,
        l.geocode_precision,
        l.latitudine,
        l.longitudine,
        ll.source as primary_source,
        ll.precision as primary_precision,
        ll.status as primary_status,
        ll.formatted_address as primary_formatted_address,
        row_number() over (
            partition by l.geocode_location_type
            order by l.pvp_id
        ) as rn
    from public.listings l
    left join public.listing_locations ll
      on ll.listing_id = l.listing_id
     and ll.is_primary = true
    where l.geocode_location_type is not null
)
select
    geocode_location_type,
    pvp_id,
    indirizzo,
    comune,
    geocode_precision,
    latitudine,
    longitudine,
    primary_source,
    primary_precision,
    primary_status,
    primary_formatted_address
from ranked
where rn <= 5
order by geocode_location_type, rn;
