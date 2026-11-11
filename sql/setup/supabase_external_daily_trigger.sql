-- Aste Easy external daily trigger via Supabase Cron -> GitHub workflow_dispatch.
-- SAFE TO COMMIT: this file contains no token.
--
-- Prerequisites in Supabase Dashboard:
--   1) Enable pg_cron (Integrations -> Cron).
--   2) Enable pg_net (Database -> Extensions).
--   3) Store a fine-grained GitHub PAT in Vault with the exact name:
--        github_aste_easy_dispatch_token
--      The PAT must be limited to repository wakingzo/aste-easy-pipeline
--      with Repository permissions -> Actions: Read and write.
--
-- Run this file only AFTER the Vault secret exists.
--
-- Schedule is UTC/GMT: 00:43 UTC every day.
-- Europe/Rome equivalent:
--   01:43 CET (winter)
--   02:43 CEST (summer)
-- Therefore it always runs before 03:00 local time.

create schema if not exists private;
revoke all on schema private from public;

do $$
begin
  if not exists (
    select 1
    from vault.decrypted_secrets
    where name = 'github_aste_easy_dispatch_token'
      and nullif(decrypted_secret, '') is not null
  ) then
    raise exception
      'Missing Supabase Vault secret: github_aste_easy_dispatch_token';
  end if;

  if to_regprocedure('net.http_post(text,jsonb,jsonb,jsonb,integer)') is null then
    raise exception 'pg_net is not enabled or net.http_post is unavailable';
  end if;

  if to_regprocedure('cron.schedule(text,text,text)') is null then
    raise exception 'pg_cron is not enabled or cron.schedule is unavailable';
  end if;
end
$$;

create or replace function private.trigger_aste_easy_daily()
returns bigint
language plpgsql
security definer
set search_path = pg_catalog, public, vault, net
as $$
declare
  v_token text;
  v_request_id bigint;
begin
  select decrypted_secret
    into v_token
    from vault.decrypted_secrets
   where name = 'github_aste_easy_dispatch_token';

  if nullif(v_token, '') is null then
    raise exception
      'Missing Supabase Vault secret: github_aste_easy_dispatch_token';
  end if;

  select net.http_post(
    url := 'https://api.github.com/repos/wakingzo/aste-easy-pipeline/actions/workflows/daily.yml/dispatches',
    headers := jsonb_build_object(
      'Accept', 'application/vnd.github+json',
      'Authorization', 'Bearer ' || v_token,
      'X-GitHub-Api-Version', '2026-03-10',
      'User-Agent', 'AsteEasy-Supabase-Cron',
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('ref', 'main'),
    timeout_milliseconds := 10000
  )
  into v_request_id;

  return v_request_id;
end;
$$;

revoke all on function private.trigger_aste_easy_daily() from public, anon, authenticated, service_role;

-- Named jobs are replaced by cron.schedule when the same name already exists.
select cron.schedule(
  'aste-easy-daily-github-dispatch',
  '43 0 * * *',
  $$select private.trigger_aste_easy_daily();$$
);

-- Verification: one active named job should be returned.
select
  jobid,
  jobname,
  schedule,
  active,
  command
from cron.job
where jobname = 'aste-easy-daily-github-dispatch';
