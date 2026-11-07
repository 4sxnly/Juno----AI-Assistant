# Downloads the native engines an app ships in bin\ (run at BUILD time, never on user PCs).
#   Multi-Tool:  pwsh -File build/fetch_engines.ps1                                   (yt-dlp, deno, ffmpeg)
#   Juno:        pwsh -File build/fetch_engines.ps1 -Only llama -OutDir juno/bin      (llama.cpp)
# Every file is checked against the publisher's SHA-256 list before it's accepted.
param(
    [string[]]$Only = @("yt-dlp", "deno", "ffmpeg"),
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest much faster

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$bin = if ($OutDir) { Join-Path $root $OutDir } else { Join-Path $root "bin" }
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "multitool-engines"
New-Item -ItemType Directory -Force -Path $bin, $tmp | Out-Null

function Get-File([string]$url, [string]$out) {
    Write-Host "  downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
}

# Checksum files come in different shapes and encodings (UTF-8, UTF-16 from PowerShell,
# "hash  filename" lists, or "Hash : ..." blocks). Normalise to plain text first.
function ConvertTo-PlainText($content) {
    if ($content -is [byte[]]) {
        $bytes = $content
        if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
            $text = [System.Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2)
        } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) {
            $text = [System.Text.Encoding]::BigEndianUnicode.GetString($bytes, 2, $bytes.Length - 2)
        } else {
            $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        }
    } else {
        $text = [string]$content
    }
    return $text.Replace([string][char]0, "")
}

function Assert-Sha256([string]$file, [string]$sumsUrl, [string]$assetName, [switch]$Optional) {
    try {
        $raw = (Invoke-WebRequest -Uri $sumsUrl -UseBasicParsing).Content
    } catch {
        if ($Optional) { Write-Warning "No checksum list at $sumsUrl - skipping verification of $assetName"; return }
        throw
    }
    $text = ConvertTo-PlainText $raw
    $hashPattern = "(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])"
    $all = @([regex]::Matches($text, $hashPattern) | ForEach-Object { $_.Value.ToLower() } | Select-Object -Unique)

    $expected = $null
    if ($all.Count -eq 1) {
        # single-asset checksum file (any layout)
        $expected = $all[0]
    } elseif ($all.Count -gt 1) {
        # multi-asset list: take the hash on the line that names this asset
        $line = ($text -split "\r?\n") |
            Where-Object { $_ -match ([regex]::Escape($assetName) + "\s*$") } |
            Select-Object -First 1
        if ($line) { $expected = [regex]::Match($line, $hashPattern).Value.ToLower() }
    }
    if (-not $expected) { throw "No checksum for $assetName in $sumsUrl" }

    $actual = (Get-FileHash -Algorithm SHA256 -Path $file).Hash.ToLower()
    if ($actual -ne $expected) { throw "Checksum mismatch for $assetName (expected $expected, got $actual)" }
    Write-Host "  sha256 OK  $assetName"
}

if ($Only -contains "yt-dlp") {
    # ── yt-dlp (official exe already contains the yt-dlp-ejs challenge scripts) ──
    Write-Host "yt-dlp"
    $yt = Join-Path $tmp "yt-dlp.exe"
    Get-File "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" $yt
    Assert-Sha256 $yt "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS" "yt-dlp.exe"
    Copy-Item $yt (Join-Path $bin "yt-dlp.exe") -Force
}

if ($Only -contains "deno") {
    # ── Deno (JavaScript runtime yt-dlp needs for YouTube) ──
    Write-Host "deno"
    $denoAsset = "deno-x86_64-pc-windows-msvc.zip"
    $denoZip = Join-Path $tmp $denoAsset
    Get-File "https://github.com/denoland/deno/releases/latest/download/$denoAsset" $denoZip
    Assert-Sha256 $denoZip "https://github.com/denoland/deno/releases/latest/download/$denoAsset.sha256sum" $denoAsset -Optional
    Expand-Archive -Path $denoZip -DestinationPath (Join-Path $tmp "deno") -Force
    Copy-Item (Join-Path $tmp "deno\deno.exe") (Join-Path $bin "deno.exe") -Force
}

if ($Only -contains "ffmpeg") {
    # ── FFmpeg (shared build: ffmpeg + ffprobe share one set of DLLs) ──
    Write-Host "ffmpeg"
    $ffAsset = "ffmpeg-master-latest-win64-gpl-shared.zip"
    $ffZip = Join-Path $tmp $ffAsset
    Get-File "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/$ffAsset" $ffZip
    Assert-Sha256 $ffZip "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256" $ffAsset -Optional
    $ffDir = Join-Path $tmp "ffmpeg"
    Expand-Archive -Path $ffZip -DestinationPath $ffDir -Force
    $ffBin = Get-ChildItem -Path $ffDir -Recurse -Directory -Filter "bin" | Select-Object -First 1
    Copy-Item (Join-Path $ffBin.FullName "ffmpeg.exe"), (Join-Path $ffBin.FullName "ffprobe.exe") $bin -Force
    Copy-Item (Join-Path $ffBin.FullName "*.dll") $bin -Force
    $license = Get-ChildItem -Path $ffDir -Recurse -File -Filter "LICENSE*" | Select-Object -First 1
    if ($license) { Copy-Item $license.FullName (Join-Path $bin "FFMPEG-LICENSE.txt") -Force }
}

if ($Only -contains "llama") {
    # ── llama.cpp server: offline AI for the Desktop Companion ──
    # Two official builds: Vulkan (uses almost any graphics card) and CPU (works everywhere).
    Write-Host "llama.cpp"
    $ghHeaders = @{ "User-Agent" = "multitool-juno-build"; "Accept" = "application/vnd.github+json" }
    if ($env:GITHUB_TOKEN) { $ghHeaders["Authorization"] = "Bearer $env:GITHUB_TOKEN" }
    $api = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
    function Test-HasWindowsBuilds($rel) {
        $names = @($rel.assets | ForEach-Object { $_.name })
        return (($names -match "-bin-win-cpu-x64\.zip$").Count -gt 0) -and (($names -match "-bin-win-vulkan-x64\.zip$").Count -gt 0)
    }
    # Stable releases (v0.x.y) ship source only; their notes name the nightly build (bNNNNN) that has the binaries.
    $release = $null
    $stable = Invoke-RestMethod -Uri "$api/latest" -Headers $ghHeaders
    if (Test-HasWindowsBuilds $stable) {
        $release = $stable
    } else {
        $nightly = [regex]::Match("$($stable.body)", "Nightly build:\W*(b\d+)").Groups[1].Value
        if ($nightly) {
            try {
                $candidate = Invoke-RestMethod -Uri "$api/tags/$nightly" -Headers $ghHeaders
                if (Test-HasWindowsBuilds $candidate) { $release = $candidate }
            } catch { Write-Warning "Nightly $nightly not found: $_" }
        }
    }
    if (-not $release) {  # fall back to the newest release that has Windows binaries
        $recent = Invoke-RestMethod -Uri "${api}?per_page=40" -Headers $ghHeaders
        $release = $recent | Where-Object { Test-HasWindowsBuilds $_ } | Select-Object -First 1
    }
    if (-not $release) { throw "Couldn't find a llama.cpp release with Windows CPU + Vulkan builds" }
    Write-Host "  using $($release.tag_name) (latest stable: $($stable.tag_name))"
    foreach ($variant in @("cpu", "vulkan")) {
        $asset = $release.assets | Where-Object { $_.name -match "^llama-.*-bin-win-$variant-x64\.zip$" } | Select-Object -First 1
        if (-not $asset) { throw "llama.cpp $($release.tag_name) has no win-$variant-x64 zip" }
        $zip = Join-Path $tmp $asset.name
        Get-File $asset.browser_download_url $zip
        if ($asset.digest -and "$($asset.digest)".StartsWith("sha256:")) {
            $expected = "$($asset.digest)".Substring(7).ToLower()
            $actual = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
            if ($actual -ne $expected) { throw "Checksum mismatch for $($asset.name)" }
            Write-Host "  sha256 OK  $($asset.name)"
        } else {
            Write-Warning "GitHub published no digest for $($asset.name)"
        }
        $unzipped = Join-Path $tmp "llama-$variant"
        if (Test-Path $unzipped) { Remove-Item $unzipped -Recurse -Force }
        Expand-Archive -Path $zip -DestinationPath $unzipped -Force
        $server = Get-ChildItem -Path $unzipped -Recurse -File -Filter "llama-server.exe" | Select-Object -First 1
        if (-not $server) { throw "llama-server.exe not found in $($asset.name)" }
        $dest = Join-Path $bin "llama-$variant"
        if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        Copy-Item (Join-Path $server.DirectoryName "*") $dest -Recurse -Force
        # keep only the server (+ its DLLs); the other llama.cpp tools aren't used
        Get-ChildItem $dest -Filter "*.exe" | Where-Object { $_.Name -ne "llama-server.exe" } | Remove-Item -Force
        # Visual C++ + OpenMP runtimes next to the exe, so no redistributable has to be installed
        foreach ($dll in @("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll",
                           "msvcp140_2.dll", "vcomp140.dll")) {
            $src = Join-Path $env:SystemRoot "System32\$dll"
            if ((Test-Path $src) -and -not (Test-Path (Join-Path $dest $dll))) { Copy-Item $src $dest }
        }
    }
    & (Join-Path $bin "llama-cpu\llama-server.exe") --version
    if ($LASTEXITCODE -ne 0) { throw "llama-server (CPU) failed its self-test" }
}

# ── Tesseract OCR: added when the Optics module (8) is built ──

Write-Host ""
Write-Host "bin\ now contains:"
Get-ChildItem $bin | Format-Table Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } } -AutoSize
if ($Only -contains "yt-dlp") { & (Join-Path $bin "yt-dlp.exe") --version }
if ($Only -contains "deno") { & (Join-Path $bin "deno.exe") --version }
if ($Only -contains "ffmpeg") { & (Join-Path $bin "ffmpeg.exe") -hide_banner -version | Select-Object -First 1 }
