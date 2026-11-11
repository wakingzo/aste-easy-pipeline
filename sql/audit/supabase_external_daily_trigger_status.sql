-- Read-only checks for the Supabase -> GitHub external daily trigger.

-- Job configuration.
select
  jobid,
  jobname,
  schedule,
  active,
  command
from cron.job
where jobname = 'aste-easy-daily-github-dispatch';

-- Recent pg_cron executions.
select
  jobid,
  runid,
  status,
  return_message,
  start_time,
  end_time
from cron.job_run_details
where jobid in (
  select jobid
  from cron.job
  where jobname = 'aste-easy-daily-github-dispatch'
)
order by start_time desc
limit 20;

-- Recent GitHub HTTP responses.
-- A successful workflow dispatch is any 2xx response.
select
  id as request_id,
  status_code,
  timed_out,
  error_msg,
  created,
  content
from net._http_response
where created > now() - interval '6 hours'
order by created desc
limit 20;
