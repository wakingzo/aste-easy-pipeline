-- Aste Easy
-- Drop legacy geocoding workflow columns from public.listings.
-- Rich geocoding state is authoritative in public.listing_locations.
-- Keep only frontend projection fields on listings:
--   latitudine, longitudine, maps_link, geocode_precision
-- Keep geocode_location_type temporarily for historical frontend fallback.

begin;

alter table public.listings
    drop column if exists geocode_formatted_address,
    drop column if exists geocode_source,
    drop column if exists geocode_query_used,
    drop column if exists pvp_latitudine,
    drop column if exists pvp_longitudine,
    drop column if exists pvp_geo_source,
    drop column if exists geocode_status,
    drop column if exists geocode_version,
    drop column if exists geocode_checked_at,
    drop column if exists geocode_input_hash,
    drop column if exists geocode_google_attempted_at,
    drop column if exists geocode_google_outcome,
    drop column if exists geocode_decision_reason;

commit;
