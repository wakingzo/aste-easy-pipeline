Aste Easy - v3.12.1 RPC prerequisite fix

WHY
The first production attempt stopped at inserted=0 because PostgREST returned PGRST202:
public.aste_insert_new_listing_v1 did not exist in the Supabase schema cache.
No listing/subject/attachment row was written by that failed attempt.

ONE-TIME DATABASE STEP
1. Supabase Dashboard -> SQL Editor -> New query.
2. Open sql/001_aste_insert_new_listing_v1.sql from this package.
3. Paste/run the entire file.
4. The final SELECT must show public | aste_insert_new_listing_v1 | p_listing jsonb, p_soggetti jsonb, p_allegati jsonb.

FAST ZERO-WRITE CHECK
  .\\python\\python.exe .\\src\\importer\\pvp_import_atomic_v1.py --check-rpc
Expected:
  [RPC CHECK] OK: public.aste_insert_new_listing_v1 visible; zero writes.

The probe cannot insert anything: it deliberately sends an empty p_listing and expects
SQLSTATE 22023 (p_listing.pvp_id is required) from inside the installed function.

PRODUCTION RETRY
The failed 27/10/2026 attempt reported inserted=0 and existing_writes=0, so there is
nothing to resume or clean up. After RPC CHECK is OK, rerun the same guarded production
apply command with expected-new 9834 and the authorized RAW SHA. The importer rebuilds
the plan and rechecks count+SHA before its first write.

v3.12.1 also probes the RPC before the expensive ANNCSU scan on every production apply,
so a missing/stale RPC will fail immediately with zero writes.
