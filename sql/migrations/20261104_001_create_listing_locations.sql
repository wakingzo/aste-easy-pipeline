-- Aste Easy
-- Experimental geocoding schema v1
-- ADDITIVE ONLY: does not alter or drop columns from public.listings.
--
-- Purpose:
--   - keep public.listings as the auction/frontend table;
--   - store one row per bene/location here;
--   - retain resolver provenance and quality without growing listings indefinitely.
--
-- Safe to run before importer migration. Existing listings rows are untouched.

begin;

create table if not exists public.listing_locations (
    location_id uuid not null default gen_random_uuid(),

    listing_id uuid not null,
    pvp_id text not null,

    bene_index integer not null default 0,
    is_primary boolean not null default false,

    -- Raw PVP address context for this bene.
    address_raw text null,
    comune_pvp text null,
    provincia_pvp text null,
    sigla_provincia_pvp text null,
    cap_pvp text null,
    codice_istat text null,

    pvp_latitudine double precision null,
    pvp_longitudine double precision null,

    -- Parsed address.
    street text null,
    civic text null,
    internal text null,
    locality text null,
    snc boolean not null default false,
    is_intersection boolean not null default false,

    -- Final resolved point for this bene/location.
    latitudine double precision null,
    longitudine double precision null,

    source text null,
    precision text null,
    confidence text null,
    status text null,
    formatted_address text null,

    -- Compact diagnostics. Detailed scoring stays in JSONL reports/cache.
    anncsu_status text null,
    google_status text null,
    warnings text[] not null default '{}'::text[],
    resolution_reason text null,
    resolver_version text null,
    checked_at timestamp with time zone null,

    created_at timestamp with time zone not null default now(),
    updated_at timestamp with time zone not null default now(),

    constraint listing_locations_pkey primary key (location_id),

    constraint listing_locations_listing_id_fkey
        foreign key (listing_id)
        references public.listings(listing_id)
        on delete cascade,

    constraint listing_locations_listing_bene_key
        unique (listing_id, bene_index),

    constraint listing_locations_bene_index_check
        check (bene_index >= 0),

    constraint listing_locations_final_coords_pair_check
        check (
            (latitudine is null and longitudine is null)
            or
            (latitudine is not null and longitudine is not null)
        ),

    constraint listing_locations_pvp_coords_pair_check
        check (
            (pvp_latitudine is null and pvp_longitudine is null)
            or
            (pvp_latitudine is not null and pvp_longitudine is not null)
        ),

    constraint listing_locations_final_lat_check
        check (latitudine is null or latitudine between -90 and 90),

    constraint listing_locations_final_lon_check
        check (longitudine is null or longitudine between -180 and 180),

    constraint listing_locations_pvp_lat_check
        check (pvp_latitudine is null or pvp_latitudine between -90 and 90),

    constraint listing_locations_pvp_lon_check
        check (pvp_longitudine is null or pvp_longitudine between -180 and 180)
);

-- Fast lookup from auction/PVP id.
create index if not exists listing_locations_pvp_id_idx
    on public.listing_locations using btree (pvp_id);

create index if not exists listing_locations_listing_id_idx
    on public.listing_locations using btree (listing_id);

-- Resolver / audit queries.
create index if not exists listing_locations_status_idx
    on public.listing_locations using btree (status);

create index if not exists listing_locations_source_idx
    on public.listing_locations using btree (source);

create index if not exists listing_locations_codice_istat_idx
    on public.listing_locations using btree (codice_istat);

-- Exactly zero or one primary location per listing.
create unique index if not exists listing_locations_one_primary_per_listing_idx
    on public.listing_locations using btree (listing_id)
    where is_primary = true;

-- Reuse the existing generic updated_at trigger function already used by listings.
drop trigger if exists trg_listing_locations_updated_at on public.listing_locations;
create trigger trg_listing_locations_updated_at
before update on public.listing_locations
for each row
execute function public.set_updated_at();

comment on table public.listing_locations is
    'One row per auction bene/location. Stores parsed PVP address, resolved coordinates and compact geocoding provenance. public.listings retains only the primary map pin.';

comment on column public.listing_locations.is_primary is
    'True for the location selected as the primary map point for the parent listing. At most one per listing.';

comment on column public.listing_locations.precision is
    'Resolver precision such as CIVICO, STRADA, LOCALITA, COMUNE or NESSUNA.';

comment on column public.listing_locations.source is
    'Winning coordinate source, e.g. ANNCSU, PVP, GOOGLE, ISTAT_LOCALITA.';

comment on column public.listing_locations.warnings is
    'Compact machine-readable warning codes. Detailed resolver evidence remains outside the relational schema.';

commit;
