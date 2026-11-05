-- Aste Easy
-- Zero-persistence validator for atomic importer v2.
--
-- Calls the real v2 RPC inside a PL/pgSQL subtransaction, captures the result,
-- then deliberately raises a private sentinel exception. The exception rolls
-- back every write done by v2; the outer handler returns the captured result.
--
-- Intended only for service_role validation of real importer payloads.

begin;

create or replace function public.aste_validate_new_listing_v2(
    p_listing jsonb,
    p_soggetti jsonb default '[]'::jsonb,
    p_allegati jsonb default '[]'::jsonb,
    p_locations jsonb default '[]'::jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_result jsonb;
begin
    begin
        v_result := public.aste_insert_new_listing_v2(
            p_listing,
            coalesce(p_soggetti, '[]'::jsonb),
            coalesce(p_allegati, '[]'::jsonb),
            coalesce(p_locations, '[]'::jsonb)
        );

        -- Deliberately abort the inner block so all v2 writes are rolled back.
        raise exception using
            errcode = 'PZV20',
            message = coalesce(v_result::text, '{}');
    exception
        when sqlstate 'PZV20' then
            return jsonb_build_object(
                'validated', true,
                'rolled_back', true,
                'rpc_result', v_result
            );
    end;
exception
    when others then
        -- Real validation errors are propagated unchanged.
        raise;
end;
$$;

revoke all on function public.aste_validate_new_listing_v2(jsonb,jsonb,jsonb,jsonb) from public;
grant execute on function public.aste_validate_new_listing_v2(jsonb,jsonb,jsonb,jsonb) to service_role;

comment on function public.aste_validate_new_listing_v2(jsonb,jsonb,jsonb,jsonb) is
    'Executes aste_insert_new_listing_v2 with real payload semantics but rolls back all writes before returning. service_role only.';

commit;
