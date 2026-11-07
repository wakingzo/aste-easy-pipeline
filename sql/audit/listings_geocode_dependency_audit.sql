-- Read-only dependency audit before dropping legacy geocoding columns from public.listings.
-- No writes.

with candidates(column_name) as (
    values
      ('geocode_formatted_address'),
      ('geocode_source'),
      ('geocode_query_used'),
      ('pvp_latitudine'),
      ('pvp_longitudine'),
      ('pvp_geo_source'),
      ('geocode_status'),
      ('geocode_version'),
      ('geocode_checked_at'),
      ('geocode_input_hash'),
      ('geocode_google_attempted_at'),
      ('geocode_google_outcome'),
      ('geocode_decision_reason')
),
functions as (
    select
        'FUNCTION'::text as object_type,
        n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' as object_name,
        c.column_name,
        pg_get_functiondef(p.oid) as definition
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    cross join candidates c
    where p.prokind in ('f','p')
      and n.nspname not in ('pg_catalog','information_schema')
      and pg_get_functiondef(p.oid) ilike '%' || c.column_name || '%'
),
views as (
    select
        'VIEW'::text as object_type,
        schemaname || '.' || viewname as object_name,
        c.column_name,
        definition
    from pg_views v
    cross join candidates c
    where v.schemaname not in ('pg_catalog','information_schema')
      and v.definition ilike '%' || c.column_name || '%'
),
matviews as (
    select
        'MATERIALIZED VIEW'::text as object_type,
        schemaname || '.' || matviewname as object_name,
        c.column_name,
        definition
    from pg_matviews v
    cross join candidates c
    where v.schemaname not in ('pg_catalog','information_schema')
      and v.definition ilike '%' || c.column_name || '%'
),
triggers as (
    select
        'TRIGGER'::text as object_type,
        n.nspname || '.' || cls.relname || '.' || t.tgname as object_name,
        c.column_name,
        pg_get_triggerdef(t.oid, true) as definition
    from pg_trigger t
    join pg_class cls on cls.oid = t.tgrelid
    join pg_namespace n on n.oid = cls.relnamespace
    cross join candidates c
    where not t.tgisinternal
      and pg_get_triggerdef(t.oid, true) ilike '%' || c.column_name || '%'
),
indexes as (
    select
        'INDEX'::text as object_type,
        schemaname || '.' || indexname as object_name,
        c.column_name,
        indexdef as definition
    from pg_indexes i
    cross join candidates c
    where i.schemaname not in ('pg_catalog','information_schema')
      and i.indexdef ilike '%' || c.column_name || '%'
)
select * from functions
union all
select * from views
union all
select * from matviews
union all
select * from triggers
union all
select * from indexes
order by column_name, object_type, object_name;
