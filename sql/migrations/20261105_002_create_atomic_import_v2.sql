-- Aste Easy
-- Atomic importer RPC v2: reuse v1 and add listing_locations in the same transaction.
--
-- Prerequisites:
--   1) public.listing_locations exists
--   2) public.aste_insert_new_listing_v1(jsonb,jsonb,jsonb) already exists
--
-- This migration does NOT replace/drop v1.

begin;

create or replace function public.aste_insert_new_listing_v2(
    p_listing jsonb,
    p_soggetti jsonb default '[]'::jsonb,
    p_allegati jsonb default '[]'::jsonb,
    p_locations jsonb default '[]'::jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_result jsonb;
    v_pvp_id text;
    v_listing_id uuid;
    v_location_count integer := 0;
begin
    -- Delegate all existing parent/children validation and insertion semantics
    -- to the already-production-tested v1 function.
    v_result := public.aste_insert_new_listing_v1(
        p_listing,
        coalesce(p_soggetti, '[]'::jsonb),
        coalesce(p_allegati, '[]'::jsonb)
    );

    v_pvp_id := nullif(v_result ->> 'pvp_id', '');

    if v_pvp_id is null then
        raise exception using
            errcode = '22023',
            message = 'aste_insert_new_listing_v2: v1 returned no pvp_id';
    end if;

    -- v1 is idempotent: when the listing already existed, do not attach
    -- a fresh location payload to an old row. The caller must treat this
    -- exactly like the existing skipped_concurrent behavior.
    if coalesce((v_result ->> 'inserted')::boolean, false) = false then
        return v_result || jsonb_build_object('locations', 0);
    end if;

    select l.listing_id
      into v_listing_id
      from public.listings l
     where l.pvp_id = v_pvp_id
     limit 1;

    if v_listing_id is null then
        raise exception using
            errcode = '23503',
            message = 'aste_insert_new_listing_v2: inserted listing not found by pvp_id';
    end if;

    if jsonb_typeof(coalesce(p_locations, '[]'::jsonb)) <> 'array' then
        raise exception using
            errcode = '22023',
            message = 'aste_insert_new_listing_v2: p_locations must be a JSON array';
    end if;

    -- Every payload row must belong to the same parent pvp_id.
    if exists (
        select 1
          from jsonb_array_elements(coalesce(p_locations, '[]'::jsonb)) x
         where nullif(x ->> 'pvp_id', '') is distinct from v_pvp_id
    ) then
        raise exception using
            errcode = '22023',
            message = 'aste_insert_new_listing_v2: location pvp_id mismatch';
    end if;

    insert into public.listing_locations (
        listing_id,
        pvp_id,
        bene_index,
        is_primary,
        address_raw,
        comune_pvp,
        provincia_pvp,
        sigla_provincia_pvp,
        cap_pvp,
        codice_istat,
        pvp_latitudine,
        pvp_longitudine,
        street,
        civic,
        internal,
        locality,
        snc,
        is_intersection,
        latitudine,
        longitudine,
        source,
        precision,
        confidence,
        status,
        formatted_address,
        anncsu_status,
        google_status,
        warnings,
        resolution_reason,
        resolver_version,
        checked_at
    )
    select
        v_listing_id,
        v_pvp_id,
        r.bene_index,
        coalesce(r.is_primary, false),
        r.address_raw,
        r.comune_pvp,
        r.provincia_pvp,
        r.sigla_provincia_pvp,
        r.cap_pvp,
        r.codice_istat,
        r.pvp_latitudine,
        r.pvp_longitudine,
        r.street,
        r.civic,
        r.internal,
        r.locality,
        coalesce(r.snc, false),
        coalesce(r.is_intersection, false),
        r.latitudine,
        r.longitudine,
        r.source,
        r.precision,
        r.confidence,
        coalesce(r.status, 'PENDING'),
        r.formatted_address,
        r.anncsu_status,
        r.google_status,
        coalesce(r.warnings, '{}'::text[]),
        r.resolution_reason,
        r.resolver_version,
        r.checked_at
    from jsonb_to_recordset(coalesce(p_locations, '[]'::jsonb)) as r(
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
        checked_at timestamp with time zone
    );

    get diagnostics v_location_count = row_count;

    -- If the listing has location rows, require exactly one primary.
    if v_location_count > 0 and (
        select count(*)
          from public.listing_locations ll
         where ll.listing_id = v_listing_id
           and ll.is_primary = true
    ) <> 1 then
        raise exception using
            errcode = '23514',
            message = 'aste_insert_new_listing_v2: expected exactly one primary location';
    end if;

    return v_result || jsonb_build_object('locations', v_location_count);
end;
$$;

revoke all on function public.aste_insert_new_listing_v2(jsonb,jsonb,jsonb,jsonb) from public;
grant execute on function public.aste_insert_new_listing_v2(jsonb,jsonb,jsonb,jsonb) to service_role;

comment on function public.aste_insert_new_listing_v2(jsonb,jsonb,jsonb,jsonb) is
    'Atomic new-listing insert v2. Reuses v1 for listing/soggetti/allegati and inserts listing_locations in the same transaction.';

commit;
