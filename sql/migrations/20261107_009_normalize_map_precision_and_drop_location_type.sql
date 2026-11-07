-- Aste Easy
-- Normalize public.listings.geocode_precision to four user-facing map classes
-- and retire legacy public.listings.geocode_location_type.
--
-- Canonical parent values:
--   PRECISA
--   APPROSSIMATIVA
--   INDICATIVA
--   DA_VERIFICARE
--
-- Rich technical precision/source/reason remains in public.listing_locations.

begin;

-- 1) Prefer an authoritative modern primary location when it is resolved and
--    represents the same coordinates currently published on the parent.
with primary_current as (
    select
        l.listing_id,
        upper(coalesce(ll.precision, '')) as technical_precision
    from public.listings l
    join public.listing_locations ll
      on ll.listing_id = l.listing_id
     and ll.is_primary = true
    where ll.status = 'RESOLVED'
      and coalesce(ll.source, '') <> 'LEGACY_LISTINGS'
      and l.latitudine is not null
      and l.longitudine is not null
      and ll.latitudine is not null
      and ll.longitudine is not null
      and abs(l.latitudine - ll.latitudine) < 0.0000001
      and abs(l.longitudine - ll.longitudine) < 0.0000001
      and upper(coalesce(ll.precision, '')) in ('CIVICO','PVP_COORD','STRADA','LOCALITA','COMUNE')
)
update public.listings l
   set geocode_precision = case p.technical_precision
       when 'CIVICO' then 'PRECISA'
       when 'PVP_COORD' then 'APPROSSIMATIVA'
       when 'STRADA' then 'APPROSSIMATIVA'
       when 'LOCALITA' then 'INDICATIVA'
       when 'COMUNE' then 'INDICATIVA'
   end
  from primary_current p
 where p.listing_id = l.listing_id;

-- 2) Normalize every remaining published parent pin. Existing geocode_precision
--    wins over geocode_location_type because it generally represents the newer
--    classification. Legacy location_type is used only as fallback.
update public.listings
   set geocode_precision = case
       when latitudine is null or longitudine is null then null

       -- Already canonical.
       when upper(coalesce(geocode_precision, '')) in
            ('PRECISA','APPROSSIMATIVA','INDICATIVA','DA_VERIFICARE')
         then upper(geocode_precision)

       -- Legacy/new technical precision values.
       when upper(coalesce(geocode_precision, '')) = 'CIVICO'
         then 'PRECISA'
       when upper(coalesce(geocode_precision, '')) in (
            'PVP_COORD',
            'PVP_CIVICO_COMPATIBILE',
            'PVP_IN_COMUNE',
            'PVP_STRADA_COMPATIBILE',
            'STRADA'
       )
         then 'APPROSSIMATIVA'
       when upper(coalesce(geocode_precision, '')) in (
            'LOCALITA',
            'COMUNE',
            'PVP_LOCALITA_COMPATIBILE'
       )
         then 'INDICATIVA'
       when upper(coalesce(geocode_precision, '')) = 'PVP_INDICATIVO_ALTRO_COMUNE'
         then 'DA_VERIFICARE'

       -- Legacy geocode_location_type fallback.
       when upper(coalesce(geocode_location_type, '')) in (
            'CIVICO',
            'ROOFTOP',
            'ANNCSU_METHOD_1',
            'ANNCSU_METHOD_2',
            'ANNCSU_METHOD_3',
            'ANNCSU_METHOD_4',
            'ANNCSU_METHOD_5'
       )
         then 'PRECISA'
       when upper(coalesce(geocode_location_type, '')) in (
            'PVP_OK_STRADA',
            'GEOMETRIC_CENTER',
            'RANGE_INTERPOLATED',
            'ANNCSU_STREET_MEDOID',
            'ANNCSU_MULTI_CIVIC_CLUSTER_METHOD_5'
       )
         then 'APPROSSIMATIVA'
       when upper(coalesce(geocode_location_type, '')) in (
            'LOCALITA',
            'COMUNE',
            'APPROXIMATE',
            'ISTAT_COMUNE_FALLBACK',
            'ANNCSU_LOCALITY_MEDOID',
            'PVP_LOCALITA_COMPATIBILE'
       )
         then 'INDICATIVA'
       when upper(coalesce(geocode_location_type, '')) in (
            'PVP_SUSPECT',
            'PVP_UNCERTAIN',
            'PVP_INDICATIVO_ALTRO_COMUNE'
       )
         then 'DA_VERIFICARE'

       -- A published pin with no known trustworthy legacy classification is
       -- deliberately conservative rather than silently promoted.
       else 'DA_VERIFICARE'
   end;

-- 3) Abort before schema changes if normalization did not converge.
do $$
begin
    if exists (
        select 1
          from public.listings
         where geocode_precision is not null
           and geocode_precision not in
               ('PRECISA','APPROSSIMATIVA','INDICATIVA','DA_VERIFICARE')
    ) then
        raise exception 'Non-canonical geocode_precision values remain; aborting migration';
    end if;
end
$$;

-- 4) Retire the legacy map classification.
alter table public.listings
    drop column if exists geocode_location_type;

-- 5) Guard the simplified parent contract.
alter table public.listings
    drop constraint if exists listings_geocode_precision_canonical_check;

alter table public.listings
    add constraint listings_geocode_precision_canonical_check
    check (
        geocode_precision is null
        or geocode_precision in
           ('PRECISA','APPROSSIMATIVA','INDICATIVA','DA_VERIFICARE')
    );

commit;
