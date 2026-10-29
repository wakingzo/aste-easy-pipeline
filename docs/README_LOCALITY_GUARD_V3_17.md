# Aste Easy locality guard v3.17

Purpose: find and repair listings whose explicit locality/frazione/contrada conflicts with the published map pin.

## 1. Build ISTAT 2021 point-locality index

```powershell
.\python\python.exe .\src\geocoding\istat_locality_index_v1.py --download
```

This is local-only. It downloads the official ISTAT `LocalitaPuntuali_21.zip`, builds `data\istat\istat_localita_2021.sqlite3`, and writes a build report.

## 2. Read-only audit

```powershell
.\python\python.exe .\src\audit\locality_consistency_audit_v3_17.py
```

ZERO DB writes. ZERO Google. Output in `runtime\reports\locality_consistency_v3_17_*.csv/json`.

Severity:
- RED: gross conflict with a safe locality reference. Candidate for guarded repair.
- AMBER: suspicious but not safe to auto-repair.
- NEEDS_REF: explicit locality detected but no safe geographic reference.
- OK: compatible with the available locality evidence.

## 3. Repair (do not run before reviewing the audit)

Dry run:

```powershell
.\python\python.exe .\src\audit\locality_repair_v3_17.py --audit ".\runtime\reports\locality_consistency_v3_17_YYYYMMDD-HHMMSS.csv"
```

Apply only after review:

```powershell
.\python\python.exe .\src\audit\locality_repair_v3_17.py --audit ".\runtime\reports\locality_consistency_v3_17_YYYYMMDD-HHMMSS.csv" --apply
```

Every PATCH is guarded by the address, previous source/precision and previous coordinates from the audit snapshot.


## v3.17.2 strict repair gate
The repair executor is deliberately stricter than the v3.17.1 audit. Automatic repair requires all of:
- severity RED and audit repair_safe=true
- ref_source = ISTAT_LOCALITA_2021
- ref_strength = EXACT_UNIQUE
- distance_to_ref_m >= 2000
- empty reference_warning

Dry run (auto-selects newest v3.17.1 CSV):
`..\..\python\python.exe .\src\audit\locality_repair_v3_17.py`

Apply only after reviewing the dry-run JSON:
`..\..\python\python.exe .\src\audit\locality_repair_v3_17.py --apply`
