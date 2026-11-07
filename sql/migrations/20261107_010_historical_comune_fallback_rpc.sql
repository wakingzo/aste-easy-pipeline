-- Atomic historical municipality fallback publisher for v4.
-- Updates the parent listing and its existing primary listing_locations row
-- in one transaction per RPC call.

create or replace function public.aste_publish_comune_fallback_v1(
    p_listing_id uuid,
    p_latitudine double precision,
    p_longitudine double precision,
    p_maps_link text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_parent_count integer;
    v_location_count integer;
begin
    update public.listings
       set latitudine = p_latitudine,
           longitudine = p_longitudine,
           maps_link = p_maps_link,
           geocode_precision = 'INDICATIVA'
     where listing_id = p_listing_id
       and geocode_precision is null;

    get diagnostics v_parent_count = row_count;

    if v_parent_count <> 1 then
        raise exception 'Parent listing not eligible or not found: %', p_listing_id;
    end if;

    update public.listing_locations
       set latitudine = p_latitudine,
           longitudine = p_longitudine,
           source = 'istat_boundary_comune_indicativo',
           precision = 'COMUNE',
           status = 'RESOLVED',
           formatted_address = null,
           resolution_reason = 'Punto rappresentativo verificato all''interno del confine comunale ISTAT 2026; non rappresenta la posizione esatta dell''immobile.'
     where listing_id = p_listing_id
       and is_primary = true;

    get diagnostics v_location_count = row_count;

    if v_location_count <> 1 then
        raise exception 'Expected exactly one primary listing_location for %; got %',
            p_listing_id, v_location_count;
    end if;

    return jsonb_build_object(
        'listing_id', p_listing_id,
        'parent_updated', v_parent_count,
        'primary_location_updated', v_location_count
    );
end;
$$;

revoke all on function public.aste_publish_comune_fallback_v1(uuid,double precision,double precision,text) from public;
grant execute on function public.aste_publish_comune_fallback_v1(uuid,double precision,double precision,text) to service_role;
