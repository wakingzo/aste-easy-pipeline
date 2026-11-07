-- Aste Easy
-- Remove legacy denormalized people columns from listings.
--
-- Canonical source for auction subjects/referents is public.listing_soggetti.
-- Intentionally no CASCADE: if any database object still depends on these
-- columns, the migration must fail and roll back instead of silently dropping
-- dependent objects.

begin;

alter table public.listings
    drop column if exists delegato_vendita,
    drop column if exists custode;

commit;
