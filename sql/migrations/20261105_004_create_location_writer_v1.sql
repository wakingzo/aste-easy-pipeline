-- Aste Easy
-- Resolver location writer v1
-- Transactionally replaces/enriches all location rows for one existing listing.
-- Does NOT update public.listings map coordinates.

begin;

create or replace function public.aste_replace_listing_locations_v1(
    p_pvp_id text,
    p_locations jsonb
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_listing_id uuid;
    v_count integer := 0;
    v_primary integer := 0;
    v_item jsonb;
    v_idx integer;
begin
    if p_pvp_id is null or p_pvp_id !~ '^[0-9]{1,19}$' then
        raise exception using errcode='22023', message='invalid pvp_id';
    end if;

    if p_locations is null or jsonb_typeof(p_locations) <> 'array' then
        raise exception using errcode='22023', message='p_locations must be a JSON array';
    end if;

    select listing_id
      into v_listing_id
      from public.listings
     where pvp_id::text = p_pvp_id
     limit 1;

    if v_listing_id is null then
        raise exception using errcode='P0002', message='listing not found for pvp_id=' || p_pvp_id;
    end if;

    if jsonb_array_length(p_locations) = 0 then
        raise exception using errcode='22023', message='p_locations cannot be empty';
    end if;

    for v_item in select value from jsonb_array_elements(p_locations)
    loop
        if coalesce(v_item->>'pvp_id','') <> p_pvp_id then
            raise exception using errcode='22023', message='location pvp_id mismatch';
        end if;

        begin
            v_idx := (v_item->>'bene_index')::integer;
        exception when others then
            raise exception using errcode='22023', message='invalid bene_index';
        end;

        if v_idx < 0 then
            raise exception using errcode='22023', message='negative bene_index';
        end if;

        if coalesce((v_item->>'is_primary')::boolean,false) then
            v_primary := v_primary + 1;
        end if;
    end loop;

    if v_primary <> 1 then
        raise exception using errcode='22023', message='expected exactly one primary location';
    end if;

    -- Resolver output is authoritative for location children of this listing.
    -- Parent listing and all unrelated child tables remain untouched.
    delete from public.listing_locations
     where listing_id = v_listing_id;

    insert into public.listing_locations (
        listing_id,pvp_id,bene_index,is_primary,
        address_raw,comune_pvp,provincia_pvp,sigla_provincia_pvp,cap_pvp,codice_istat,
        pvp_latitudine,pvp_longitudine,
        street,civic,internal,locality,snc,is_intersection,
        latitudine,longitudine,source,precision,confidence,status,formatted_address,
        anncsu_status,google_status,warnings,resolution_reason,resolver_version,checked_at
    )
    select
        v_listing_id,
        pvp_id,
        bene_index,
        is_primary,
        address_raw,comune_pvp,provincia_pvp,sigla_provincia_pvp,cap_pvp,codice_istat,
        pvp_latitudine,pvp_longitudine,
        street,civic,internal,locality,coalesce(snc,false),coalesce(is_intersection,false),
        latitudine,longitudine,source,precision,confidence,status,formatted_address,
        anncsu_status,google_status,coalesce(warnings,'{}'::text[]),
        resolution_reason,resolver_version,checked_at
    from jsonb_to_recordset(p_locations) as x(
        pvp_id text,
        bene_index integer,
        is_primary boolean,
        address_raw text,
        comune_pvp text,
        provincia_pvp text,
        sigla_provincia_pvp text,
        cap_pvp text,
        codice_istat text,
        pvp_latitudine double precision,
        pvp_longitudine double precision,
        street text,
        civic text,
        internal text,
        locality text,
        snc boolean,
        is_intersection boolean,
        latitudine double precision,
        longitudine double precision,
        source text,
        precision text,
        confidence text,
        status text,
        formatted_address text,
        anncsu_status text,
        google_status text,
        warnings text[],
        resolution_reason text,
        resolver_version text,
        checked_at timestamptz
    );

    get diagnostics v_count = row_count;

    if v_count <> jsonb_array_length(p_locations) then
        raise exception using errcode='22023', message='inserted location count mismatch';
    end if;

    return jsonb_build_object(
        'pvp_id', p_pvp_id,
        'listing_id', v_listing_id,
        'locations', v_count,
        'primary_locations', v_primary
    );
end;
$$;

revoke all on function public.aste_replace_listing_locations_v1(text,jsonb) from public;
revoke all on function public.aste_replace_listing_locations_v1(text,jsonb) from anon;
revoke all on function public.aste_replace_listing_locations_v1(text,jsonb) from authenticated;
grant execute on function public.aste_replace_listing_locations_v1(text,jsonb) to service_role;


create or replace function public.aste_validate_replace_listing_locations_v1(
    p_pvp_id text,
    p_locations jsonb
) returns jsonb
language plpgsql
security definer
set search_path = public
as $validator$
declare
    v_result jsonb;
begin
    begin
        v_result := public.aste_replace_listing_locations_v1(p_pvp_id, p_locations);
        raise exception using errcode='P0001', message='ASTE_EASY_VALIDATION_ROLLBACK';
    exception
        when sqlstate 'P0001' then
            if sqlerrm <> 'ASTE_EASY_VALIDATION_ROLLBACK' then
                raise;
            end if;
    end;
    return jsonb_build_object(
        'validated', true,
        'rolled_back', true,
        'rpc_result', v_result
    );
end;
$validator$;

revoke all on function public.aste_validate_replace_listing_locations_v1(text,jsonb) from public;
revoke all on function public.aste_validate_replace_listing_locations_v1(text,jsonb) from anon;
revoke all on function public.aste_validate_replace_listing_locations_v1(text,jsonb) from authenticated;
grant execute on function public.aste_validate_replace_listing_locations_v1(text,jsonb) to service_role;

commit;
