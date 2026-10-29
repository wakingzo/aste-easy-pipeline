# Aste Easy — Google preview priority v3.5

Overlay for the existing consolidated v3 pipeline. It remains PREVIEW-ONLY:
`--apply` is still blocked, the builder is not started, and Supabase is not
written by `--google-preview`.

## Priority order

Google eligibility is still decided by the v3.4 parser/gate after PVP + ISTAT + ANNCSU.
Only eligible, specific, unambiguous addresses enter the paid queue.

1. `P1_DISCORDANZA_PVP_ANNCSU`
2. `P2_SENZA_PVP_CON_CIVICO`
3. `P3_PVP_NON_CONFERMATO_CON_CIVICO`
4. `P4_SENZA_PVP_TOPONIMO`
5. `P5_PVP_NON_CONFERMATO_TOPONIMO`

Queries are deduplicated after normalization and earlier `google_comparison_v3_*.csv`
reports are excluded. The existing Google cache remains mandatory.

## Spending guard

`--google-max-requests` is a hard per-run HTTP-attempt cap, now allowed up to 500.
`--google-max-cases` is also explicit and capped at 500. Autocomplete and Address
Validation are disabled by the preview loader. HTTP retries remain set to 1 and
all network attempts count against the per-run cap.

## Diagnostic fix

For PVP↔ANNCSU REVIEW cases, Google distance is now computed against the original
PVP coordinates even when the offline REVIEW decision intentionally has no
published coordinates. The CSV contains both `google_distance_from_pvp_m` and
`google_distance_from_offline_m`.

## Recommended run for the 2026-10-26 RAW

```powershell
cd H:\PVP_Supabase\tools
.\python\python.exe .\tests\test_google_eligibility_v3_2.py
.\python\python.exe .\tests\test_google_preview_v3.py
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-preview --google-max-requests 500 --google-max-cases 500
```

The last command can make up to 500 billable Google Geocoding HTTP attempts.
It does not write Supabase and does not run the PVP builder.
