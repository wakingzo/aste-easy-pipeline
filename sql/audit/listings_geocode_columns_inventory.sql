-- Read-only audit of geography/geocoding columns currently present on public.listings.
-- No writes.

with geo_columns as (
    select
        c.ordinal_position,
        c.column_name,
        c.data_type,
        c.udt_name
    from information_schema.columns c
    where c.table_schema = 'public'
      and c.table_name = 'listings'
      and (
          c.column_name ilike 'geocode%'
          or c.column_name ilike '%latitud%'
          or c.column_name ilike '%longitud%'
          or c.column_name in ('maps_link','pvp_geo_source')
      )
),
usage as (
    select
        e.key as column_name,
        count(*) filter (
            where e.value <> 'null'::jsonb
              and e.value <> '""'::jsonb
        ) as populated_rows
    from public.listings l
    cross join lateral jsonb_each(to_jsonb(l)) e
    where (
        e.key ilike 'geocode%'
        or e.key ilike '%latitud%'
        or e.key ilike '%longitud%'
        or e.key in ('maps_link','pvp_geo_source')
    )
    group by e.key
)
select
    g.ordinal_position,
    g.column_name,
    g.data_type,
    g.udt_name,
    coalesce(u.populated_rows,0) as populated_rows,
    (select count(*) from public.listings) as total_rows
from geo_columns g
left join usage u using (column_name)
order by g.ordinal_position;
