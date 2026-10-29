$ErrorActionPreference = "Stop"

$root = "H:\PVP_Supabase\tools"
$out  = "H:\PVP_Supabase\cloud_seed"
$ann  = Join-Path $root "data\anncsu\anncsu_runtime.sqlite3"

if (-not (Test-Path $ann)) {
    throw "Manca $ann"
}

if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Path $out | Out-Null

# ------------------------------------------------------------------
# 1) Split ANNCSU below GitHub Release's 2 GiB per-asset limit.
#    1.9 GB decimal leaves a comfortable margin.
# ------------------------------------------------------------------
$chunkSize = 1900000000
$bufferSize = 8MB
$buffer = New-Object byte[] $bufferSize
$input = [System.IO.File]::OpenRead($ann)
try {
    $part = 1
    while ($input.Position -lt $input.Length) {
        $partPath = Join-Path $out ("anncsu_runtime.sqlite3.part{0:D3}" -f $part)
        $output = [System.IO.File]::Create($partPath)
        try {
            $written = [int64]0
            while ($written -lt $chunkSize -and $input.Position -lt $input.Length) {
                $want = [int][Math]::Min($buffer.Length, $chunkSize - $written)
                $read = $input.Read($buffer, 0, $want)
                if ($read -le 0) { break }
                $output.Write($buffer, 0, $read)
                $written += $read
            }
        }
        finally { $output.Dispose() }
        Write-Host "Creato $partPath ($written bytes)"
        $part++
    }
}
finally { $input.Dispose() }

# ------------------------------------------------------------------
# 2) Mutable state seed. We preserve Google cache/counters, historical
#    geocache and reports, but NOT runtime/raw (~GB and reproducible).
# ------------------------------------------------------------------
$stage = Join-Path $out "state_seed"
New-Item -ItemType Directory -Path $stage | Out-Null

function Copy-Rel([string]$rel) {
    $src = Join-Path $root $rel
    if (-not (Test-Path $src)) { throw "Manca il file richiesto: $src" }
    $dst = Join-Path $stage $rel
    $parent = Split-Path $dst -Parent
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    if ((Get-Item $src).PSIsContainer) {
        Copy-Item $src $dst -Recurse -Force
    } else {
        Copy-Item $src $dst -Force
    }
}

Copy-Rel "runtime\cache\google_geocode_cache_v8.json"
Copy-Rel "runtime\cache\google_api_usage_v9_2.json"
Copy-Rel "runtime\cache\historical_geocache_v1.sqlite3"
Copy-Rel "runtime\state\google_geocoding_usage_v1.json"
Copy-Rel "runtime\reports"

# Never include raw PVP payloads or secrets.
Get-ChildItem $stage -Recurse -Force -File |
    Where-Object { $_.Name -match '^\.env' -or $_.Name -eq 'start_pipeline.py' } |
    Remove-Item -Force

$stateZip = Join-Path $out "aste-easy-state-seed.zip"
Compress-Archive -Path "$stage\*" -DestinationPath $stateZip -CompressionLevel Optimal
Remove-Item $stage -Recurse -Force

# ------------------------------------------------------------------
# 3) Checksums + inventory
# ------------------------------------------------------------------
Get-ChildItem $out -File | ForEach-Object {
    $h = Get-FileHash $_.FullName -Algorithm SHA256
    [PSCustomObject]@{
        File = $_.Name
        Bytes = $_.Length
        SHA256 = $h.Hash.ToLower()
    }
} | Format-Table -AutoSize | Tee-Object -FilePath (Join-Path $out "SHA256SUMS.txt")

Write-Host ""
Write-Host "Seed pronto in: $out"
Write-Host "Carica nella Release GitHub con tag: cloud-seed"
