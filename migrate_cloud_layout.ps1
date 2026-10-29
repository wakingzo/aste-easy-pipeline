$ErrorActionPreference = "Stop"

$Root = (Get-Location).Path
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Archive = Join-Path $Root "old\pre_cloud_layout_$Stamp"
$ArchiveCode = Join-Path $Archive "code"
$ArchiveDocs = Join-Path $Archive "docs"

function Require-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "File richiesto non trovato: $Path"
    }
}

# Package sanity before touching current installation.
$RequiredNew = @(
    ".\src\aste_paths.py",
    ".\src\project_env.py",
    ".\src\pipeline\daily_pipeline.py",
    ".\src\importer\build_listings_from_pvp_api_RAW_ONLY.py",
    ".\src\importer\pvp_import_incremental_v12_3.py",
    ".\src\importer\pvp_import_from_api_raw_hybrid_FINAL.py",
    ".\src\geocoding\geocode_unified_v11_6_4_plan.py",
    ".\src\geocoding\geocode_unified_v11_6_4_live.py",
    ".\tests\test_geocode_v11_6_4_regressions.py",
    ".\tests\test_layout_paths.py"
)
foreach ($f in $RequiredNew) { Require-File $f }
Require-File ".\python\python.exe"

Write-Host "[PRECHECK] Regression suite nuova struttura..." -ForegroundColor Cyan
& .\python\python.exe .\tests\test_geocode_v11_6_4_regressions.py
if ($LASTEXITCODE -ne 0) { throw "Regression test fallito PRIMA della migrazione." }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " ASTE EASY - MIGRAZIONE CLOUD-READY" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Verranno creati: src / data / runtime / tests / docs"
Write-Host "I file correnti di codice saranno ARCHIVIATI in:"
Write-Host "  $Archive"
Write-Host "I dataset grandi verranno SPOSTATI, non copiati."
Write-Host "start_pipeline.py e google_geocoding_api_key.bat restano in root"
Write-Host "come compatibilità locale temporanea."
Write-Host ""
$Confirm = Read-Host "Scrivi MIGRA per procedere"
if ($Confirm -cne "MIGRA") {
    Write-Host "Annullato. Nessun file spostato." -ForegroundColor Yellow
    exit 0
}

$Dirs = @(
    ".\data\reference",
    ".\data\anncsu",
    ".\runtime\cache",
    ".\runtime\raw",
    ".\runtime\handoff",
    ".\runtime\reports",
    $ArchiveCode,
    $ArchiveDocs
)
foreach ($d in $Dirs) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

$Moved = New-Object System.Collections.Generic.List[object]

function Move-Tracked([string]$SourceRel, [string]$DestDirRel) {
    $Source = Join-Path $Root $SourceRel
    if (-not (Test-Path -LiteralPath $Source)) {
        Write-Host "[MISSING/SKIP] $SourceRel" -ForegroundColor DarkGray
        return
    }
    $DestDir = if ([System.IO.Path]::IsPathRooted($DestDirRel)) { $DestDirRel } else { Join-Path $Root $DestDirRel }
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    $Name = Split-Path $Source -Leaf
    $Dest = Join-Path $DestDir $Name
    if (Test-Path -LiteralPath $Dest) {
        throw "Destinazione già esistente, migrazione fermata: $Dest"
    }
    Move-Item -LiteralPath $Source -Destination $Dest
    $Moved.Add([pscustomobject]@{ Source=$Source; Dest=$Dest }) | Out-Null
    Write-Host "[MOVE] $SourceRel -> $DestDirRel" -ForegroundColor Green
}

function Rollback-Moves {
    Write-Host ""; Write-Host "[ROLLBACK] Ripristino file già spostati..." -ForegroundColor Yellow
    for ($i = $Moved.Count - 1; $i -ge 0; $i--) {
        $m = $Moved[$i]
        if ((Test-Path -LiteralPath $m.Dest) -and -not (Test-Path -LiteralPath $m.Source)) {
            Move-Item -LiteralPath $m.Dest -Destination $m.Source
            Write-Host "[RESTORED] $($m.Source)" -ForegroundColor Yellow
        }
    }
}

try {
    # Reference/geodata
    Move-Tracked "Elenco-comuni-italiani.xlsx" ".\data\reference"
    Move-Tracked "comuni_cap.csv" ".\data\reference"
    Move-Tracked "Limiti01012026.zip" ".\data\reference"
    Move-Tracked "istat_confini_2026" ".\data\reference"
    Move-Tracked "STRAD_ITA_20260803.csv" ".\data\anncsu"
    Move-Tracked "INDIR_ITA_20260803.csv" ".\data\anncsu"

    # Persistent runtime state
    Move-Tracked "google_geocode_cache_v8.json" ".\runtime\cache"
    Move-Tracked "google_api_usage_v9_2.json" ".\runtime\cache"
    Move-Tracked "google_geocode_repair_cursor_v9.json" ".\runtime\cache"

    # Current PVP raw state
    Move-Tracked "listings_api_raw.jsonl" ".\runtime\raw"
    Move-Tracked "allegati_links.jsonl" ".\runtime\raw"
    Move-Tracked "listing_soggetti.jsonl" ".\runtime\raw"
    Move-Tracked "api_errors.jsonl" ".\runtime\raw"
    Move-Tracked "hybrid_api_rows_full.jsonl" ".\runtime\raw"

    # Existing run logs/audits if present
    Get-ChildItem -LiteralPath $Root -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^google_geocode_(log|audit)_v8' } |
        ForEach-Object { Move-Tracked $_.Name ".\runtime\reports" }

    # Archive root code now replaced by src/ versions.
    $OldCode = @(
        "build_listings_from_pvp_api_RAW_ONLY.py",
        "pvp_import_incremental_v12_3.py",
        "pvp_import_from_api_raw_hybrid_FINAL.py",
        "geocode_unified_v11_6_4_plan.py",
        "geocode_unified_v11_6_4_live.py",
        "google_geocode_core_v9_3_6.py",
        "anncsu_runtime_v11_2.py",
        "anncsu_local_engine_v10.py",
        "audit_db_apifree_anncsu_v2_4.py",
        "geo_admin_canonical_v11_3.py",
        "istat_boundary_runtime_v11_6.py",
        "test_geocode_v11_6_4_regressions.py",
        "test_geo_admin_canonical_v11_3.py"
    )
    foreach ($f in $OldCode) { Move-Tracked $f $ArchiveCode }

    Move-Tracked "README_RELEASE_v11_6_4.txt" ".\docs"

    # Python bytecode is disposable.
    if (Test-Path -LiteralPath ".\__pycache__") {
        Remove-Item -LiteralPath ".\__pycache__" -Recurse -Force
        Write-Host "[DELETE] __pycache__ (rigenerabile)" -ForegroundColor DarkGray
    }

    Write-Host ""
    Write-Host "[VERIFY] Controllo layout..." -ForegroundColor Cyan
    & .\python\python.exe .\tests\test_layout_paths.py
    if ($LASTEXITCODE -ne 0) { throw "test_layout_paths.py fallito" }

    Write-Host "[VERIFY] Regression suite dopo migrazione..." -ForegroundColor Cyan
    & .\python\python.exe .\tests\test_geocode_v11_6_4_regressions.py
    if ($LASTEXITCODE -ne 0) { throw "Regression suite fallita dopo migrazione" }

    $Manifest = Join-Path $Archive "migration_manifest.csv"
    $Moved | Export-Csv -NoTypeInformation -Encoding UTF8 -Path $Manifest

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " MIGRAZIONE COMPLETATA" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "Manifest: $Manifest"
    Write-Host ""
    Write-Host "Test giornaliero DRY RUN (quando vorrai provarlo):" -ForegroundColor Cyan
    Write-Host ".\python\python.exe .\src\pipeline\daily_pipeline.py"
    Write-Host ""
    Write-Host "ROOT FINALE:" -ForegroundColor Cyan
    Get-ChildItem -Force | Sort-Object Name | Select-Object -ExpandProperty Name
}
catch {
    Write-Host ""; Write-Host "ERRORE MIGRAZIONE: $($_.Exception.Message)" -ForegroundColor Red
    Rollback-Moves
    throw
}
