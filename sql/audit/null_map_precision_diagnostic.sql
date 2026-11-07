-- Read-only diagnostic for listings with NULL parent map precision.
-- Single statement: safe to paste/run as-is in Supabase.

with loc_stats as (
    select
        ll.listing_id,
        count(*) as location_rows,
        count(*) filter (
            where nullif(trim(coalesce(ll.comune_pvp,'')),'') is not null
        ) as rows_with_comune_pvp,
        count(*) filter (
            where ll.latitudine is not null and ll.longitudine is not null
        ) as rows_with_coords
    from public.listing_locations ll
    group by ll.listing_id
)
select
    case
      when l.latitudine is not null and l.longitudine is not null
        then 'A_PARENT_HAS_COORDS_BUT_PRECISION_NULL'
      when coalesce(ls.location_rows,0)=0
        then 'B_NO_LOCATION_ROWS'
      when nullif(trim(coalesce(l.comune,'')),'') is not null
        then 'C_PARENT_COMUNE_AVAILABLE'
      when coalesce(ls.rows_with_comune_pvp,0)>0
        then 'D_CHILD_COMUNE_AVAILABLE'
      else 'E_NO_COMUNE_FOUND'
    end as bucket,
    count(*) as rows
from public.listings l
left join loc_stats ls
  on ls.listing_id = l.listing_id
where l.geocode_precision is null
group by 1
order by 1;
