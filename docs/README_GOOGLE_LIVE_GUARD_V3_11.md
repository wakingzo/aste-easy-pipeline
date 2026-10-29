# Google live guard v3.11

This release is still **NO Supabase writes**. It exists only to prove that the real
Google caller is protected by the same monthly budget before production apply.

Safety chain:

1. computes a conservative current-month usage floor from local diagnostic reports,
   the v3.10 app ledger, and an optional manual override;
2. reads the existing `google_api_usage_v9_2.json` counter used by the Google core;
3. raises (never lowers) the core external offset so effective usage is at least the floor;
4. sets `MAX_MONTH_GEOCODING_CALLS` before importing the Google core;
5. the core counts/persists every network attempt **before** `urlopen`;
6. Autocomplete and Address Validation fallbacks remain disabled;
7. only fresh, post-ANNCSU eligible P1->P5 queries are selected;
8. after the run the secondary ledger is synchronized upward;
9. no DB apply path is enabled.

Recommended smoke command (10 requests max):

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-live-guard-smoke --google-live-max-requests 10 --google-monthly-cap 9000
```

Expected output: `google_live_guard_v3_11_*.csv` plus the normal atomic preview JSON.
