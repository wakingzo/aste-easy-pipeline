-- Read-only diagnostic for listings with NULL parent map precision.
-- Goal: distinguish genuinely ungeocodable records from historical/backfill gaps.

with null_parent as (
    select
        l.listing_id,
        l.pvp_id,
        l.comune,
        l.provincia,
        l.indirizzo,
        l.latitudine,
        l.longitudine,
        l.maps_link
    from public.listings l
    where l.geocode_precision is null
),
loc_stats as (
    select
        ll.listing_id,
        count(*) as location_rows,
        count(*) filter (where ll.is_primary) as primary_rows,
        count(*) filter (
            where nullif(trim(coalesce(ll.comune_pvp,'')),'') is not null
        ) as rows_with_comune_pvp,
        count(*) filter (
            where ll.latitudine is not null and ll.longitudine is not null
        ) as rows_with_coords,
        max(ll.status) filter (where ll.is_primary) as primary_status,
        max(ll.source) filter (where ll.is_primary) as primary_source,
        max(ll.precision) filter (where ll.is_primary) as primary_precision,
        max(ll.comune_pvp) filter (where ll.is_primary) as primary_comune_pvp,
        max(ll.address_raw) filter (where ll.is_primary) as primary_address_raw
    from public.listing_locations ll
    group by ll.listing_id
)
select
    case
      when np.latitudine is not null and np.longitudine is not null
        then 'A_PARENT_HAS_COORDS_BUT_PRECISION_NULL'
      when coalesce(ls.location_rows,0)=0
        then 'B_NO_LOCATION_ROWS'
      when nullif(trim(coalesce(np.comune,'')),'') is not null
        then 'C_PARENT_COMUNE_AVAILABLE'
      when coalesce(ls.rows_with_comune_pvp,0)>0
        then 'D_CHILD_COMUNE_AVAILABLE'
      else 'E_NO_COMUNE_FOUND'
    end as bucket,
    count(*) as rows
from null_parent np
left join loc_stats ls on ls.listing_id=np.listing_id
group by 1
order by 1;

-- Sample rows for inspection.
with null_parent as (
    select
        l.listing_id,
        l.pvp_id,
        l.comune,
        l.provincia,
        l.indirizzo,
        l.latitudine,
        l.longitudine,
        l.maps_link
    from public.listings l
    where l.geocode_precision is null
),
loc_stats as (
    select
        ll.listing_id,
        count(*) as location_rows,
        count(*) filter (where ll.is_primary) as primary_rows,
        count(*) filter (
            where nullif(trim(coalesce(ll.comune_pvp,'')),'') is not null
        ) as rows_with_comune_pvp,
        count(*) filter (
            where ll.latitudine is not null and ll.longitudine is not null
        ) as rows_with_coords,
        max(ll.status) filter (where ll.is_primary) as primary_status,
        max(ll.source) filter (where ll.is_primary) as primary_source,
        max(ll.precision) filter (where ll.is_primary) as primary_precision,
        max(ll.comune_pvp) filter (where ll.is_primary) as primary_comune_pvp,
        max(ll.address_raw) filter (where ll.is_primary) as primary_address_raw
    from public.listing_locations ll
    group by ll.listing_id
)
select
    np.pvp_id,
    np.comune as parent_comune,
    np.provincia as parent_provincia,
    np.indirizzo as parent_indirizzo,
    np.latitudine as parent_latitudine,
    np.longitudine as parent_longitudine,
    np.maps_link,
    coalesce(ls.location_rows,0) as location_rows,
    coalesce(ls.primary_rows,0) as primary_rows,
    coalesce(ls.rows_with_comune_pvp,0) as rows_with_comune_pvp,
    coalesce(ls.rows_with_coords,0) as rows_with_coords,
    ls.primary_status,
    ls.primary_source,
    ls.primary_precision,
    ls.primary_comune_pvp,
    ls.primary_address_raw
from null_parent np
left join loc_stats ls on ls.listing_id=np.listing_id
order by
    case
      when np.latitudine is not null and np.longitudine is not null then 0
      when coalesce(ls.location_rows,0)=0 then 1
      when nullif(trim(coalesce(np.comune,'')),'') is not null then 2
      when coalesce(ls.rows_with_comune_pvp,0)>0 then 3
      else 4
    end,
    np.pvp_id
limit 200;
