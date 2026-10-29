Aste Easy production v3.12.3 - safe resume after Supabase transport reset

- Import phase is NOT repeated during --production-resume-v312.
- Resume fetches only authorized IDs still geocode_status=PENDING.
- Google decisions are cache-first; the failed v3.12 run saved Google cache before DB PATCH.
- Supabase GET requests retry transient transport errors.
- Guarded PATCH handles unknown-commit windows: after ConnectionReset/timeout it performs a read-back; if already committed and matching, it succeeds; if still PENDING, it retries up to 4 attempts.
- Progress is printed every 100 PATCHes.
