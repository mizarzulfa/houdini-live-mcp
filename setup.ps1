# ============================================================================
# houdini-live-mcp setup (Windows)
# ----------------------------------------------------------------------------
# Automates the whole install:
#   1. finds uv and prepares the server environment (uv sync)
#   2. copies the two Houdini-side files into your Houdini settings folder
#   3. finds Claude Desktop's config file (wherever your install keeps it)
#      and registers both servers, backing up the old config first
#
# Safe to run again at any time; it just re-applies everything.
# Everything you see on screen is also saved to setup-log.txt next to
# this script, so errors are never lost if the window closes.
#
# Usage:  right-click this file -> Run with PowerShell
#   or:   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#   add -DryRun to preview without changing anything.
# ============================================================================
param([switch]$DryRun)

$ErrorActionPreference = "Stop"

function Done($msg) { Write-Host ("  OK    " + $msg) -ForegroundColor Green }
function Info($msg) { Write-Host ("  note  " + $msg) -ForegroundColor Yellow }
function Fail($msg) { throw ("SETUP: " + $msg) }

$script:ok = $false
try { Start-Transcript -Path (Join-Path $PSScriptRoot "setup-log.txt") -Force | Out-Null } catch {}

try {
    Write-Host ""
    Write-Host "houdini-live-mcp setup" -ForegroundColor Cyan
    if ($DryRun) { Write-Host "(dry run: nothing will be changed)" -ForegroundColor Yellow }
    Write-Host ""

    # --- 1. find uv (install it automatically if missing) -------------------
    function Find-Uv {
        $c = Get-Command uv.exe -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
        if (Test-Path "$env:USERPROFILE\.local\bin\uv.exe") { return "$env:USERPROFILE\.local\bin\uv.exe" }
        return $null
    }
    $uv = Find-Uv
    if (-not $uv) {
        if ($DryRun) {
            Info "uv is not installed; a real run would install it automatically"
            $uv = "$env:USERPROFILE\.local\bin\uv.exe"
        } else {
            Info "uv is not installed yet; installing it now (official installer from astral.sh)..."
            # Run in a child PowerShell so the installer's own output and error
            # handling can't disturb this script. Judge success by finding uv after.
            $prevEAP = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command `
                "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://astral.sh/uv/install.ps1 | iex" 2>&1 |
                ForEach-Object { Write-Host ("    " + $_) }
            $ErrorActionPreference = $prevEAP
            $uv = Find-Uv
            if (-not $uv) {
                Fail "automatic uv install did not work (no internet?). Install it manually from https://docs.astral.sh/uv/ then run this again."
            }
            Done "installed uv"
        }
    }
    Done "found uv: $uv"

    # --- 2. locate the server folder (it sits next to this script) ----------
    $server = Join-Path $PSScriptRoot "server"
    if (-not (Test-Path (Join-Path $server "houdini_live_mcp.py"))) {
        Fail "the 'server' folder is not next to this script. Keep setup.ps1 inside the houdini-live-mcp folder."
    }
    Done "found server folder: $server"

    # --- 3. prepare the Python environment ----------------------------------
    if (-not $DryRun) {
        # uv prints normal progress to stderr; under Stop preference that would
        # look like a fatal error. Relax while it runs, judge by exit code only.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $uvOutput = & $uv sync --directory "$server" 2>&1
        $uvExit = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        if ($uvExit -ne 0) {
            $uvOutput | ForEach-Object { Write-Host ("    " + $_) }
            Fail "uv sync failed (exit $uvExit). Its output is above and in setup-log.txt."
        }
    }
    Done "server environment ready (uv sync)"

    # --- 4. copy the two Houdini files --------------------------------------
    $docs = [Environment]::GetFolderPath("MyDocuments")
    $prefDirs = Get-ChildItem $docs -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^houdini\d+\.\d+$' }
    if (-not $prefDirs) {
        Fail "no Houdini settings folder (like houdini21.0) found in $docs. Start Houdini once, close it, and run this again."
    }
    $pref = ($prefDirs | Sort-Object { [version]($_.Name -replace 'houdini', '') } | Select-Object -Last 1).FullName
    Done "found Houdini settings: $pref"

    $bridgeSrc  = Join-Path $PSScriptRoot "houdini\scripts\python\houdini_mcp_bridge.py"
    $uireadySrc = Join-Path $PSScriptRoot "houdini\python3.11libs\uiready.py"
    $bridgeDst  = Join-Path $pref "scripts\python\houdini_mcp_bridge.py"
    $uireadyDst = Join-Path $pref "python3.11libs\uiready.py"

    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path (Split-Path $bridgeDst)  | Out-Null
        New-Item -ItemType Directory -Force -Path (Split-Path $uireadyDst) | Out-Null
        Copy-Item $bridgeSrc $bridgeDst -Force
    }
    Done "installed bridge: $bridgeDst"

    $foreignUiready = (Test-Path $uireadyDst) -and
        -not (Select-String -Path $uireadyDst -Pattern "houdini_mcp_bridge" -Quiet)
    if ($foreignUiready) {
        Info "you already have a different uiready.py there. NOT overwriting it."
        Info "see docs/DEVELOPMENT.md for how to combine the two files."
    } else {
        if (-not $DryRun) { Copy-Item $uireadySrc $uireadyDst -Force }
        Done "installed auto-start: $uireadyDst"
    }

    # --- 5. register both servers in Claude Desktop's config ----------------
    # The config location depends on how Claude Desktop was installed
    # (normal installer vs Microsoft Store), so probe both.
    $candidates = @("$env:APPDATA\Claude")
    Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -Filter "Claude_*" -ErrorAction SilentlyContinue |
        ForEach-Object { $candidates += (Join-Path $_.FullName "LocalCache\Roaming\Claude") }
    $claudeDir = $candidates | Where-Object { Test-Path $_ } |
        Sort-Object { (Get-Item $_).LastWriteTime } | Select-Object -Last 1
    if (-not $claudeDir) {
        Fail "Claude Desktop's settings folder was not found. Install Claude Desktop, open it once, then run this again."
    }
    $cfgPath = Join-Path $claudeDir "claude_desktop_config.json"

    $cfg = [pscustomobject]@{}
    if (Test-Path $cfgPath) {
        try { $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json }
        catch { Fail "your existing $cfgPath is not valid JSON. Fix or delete it, then run this again." }
    }
    if (-not $cfg.PSObject.Properties["mcpServers"]) {
        $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
    }
    $entryDocs = [pscustomobject]@{ command = $uv; args = @("--directory", $server, "run", "houdini_docs_mcp.py") }
    $entryLive = [pscustomobject]@{ command = $uv; args = @("--directory", $server, "run", "houdini_live_mcp.py") }
    $cfg.mcpServers | Add-Member -NotePropertyName "houdini-docs" -NotePropertyValue $entryDocs -Force
    $cfg.mcpServers | Add-Member -NotePropertyName "houdini-live" -NotePropertyValue $entryLive -Force

    if (-not $DryRun) {
        if (Test-Path $cfgPath) { Copy-Item $cfgPath "$cfgPath.bak" -Force }
        $json = $cfg | ConvertTo-Json -Depth 10
        # WriteAllText writes UTF-8 without a BOM; a BOM can break JSON parsers.
        [System.IO.File]::WriteAllText($cfgPath, $json)
        # PowerShell 5's JSON formatting is ugly (column-aligned indentation).
        # Reformat with the Python that uv sync just installed; best effort,
        # the file is already valid JSON either way.
        # -I (isolated) shields it from any PYTHONHOME/PYTHONPATH the user has
        # set globally for DCC tools, which would otherwise break the venv.
        $py = Join-Path $server ".venv\Scripts\python.exe"
        if (Test-Path $py) {
            try {
                & $py -I -c "import json,sys; p=sys.argv[1]; d=json.load(open(p,encoding='utf-8-sig')); open(p,'w',encoding='utf-8').write(json.dumps(d,indent=2))" $cfgPath 2>$null
            } catch {}
        }
        Info "previous config backed up to claude_desktop_config.json.bak"
    }
    Done "registered both servers in: $cfgPath"

    # --- done ----------------------------------------------------------------
    Write-Host ""
    Write-Host "Setup complete." -ForegroundColor Cyan
    Write-Host "Next: restart Claude Desktop, start Houdini, then in a chat click the"
    Write-Host "+ (plus) button -> Connectors -> switch houdini-docs and houdini-live on."
    Write-Host ""
    $script:ok = $true
}
catch {
    Write-Host ""
    $msg = $_.Exception.Message
    if ($msg -like "SETUP: *") {
        Write-Host ("  FAIL  " + $msg.Substring(7)) -ForegroundColor Red
    } else {
        Write-Host "  FAIL  unexpected error:" -ForegroundColor Red
        Write-Host ("  " + $msg) -ForegroundColor Red
        if ($_.InvocationInfo) { Write-Host $_.InvocationInfo.PositionMessage -ForegroundColor Red }
    }
    Write-Host ""
    Write-Host "  This whole run was saved to setup-log.txt next to this script."
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
    try { Read-Host "Press Enter to close" | Out-Null } catch {}
}
if (-not $script:ok) { exit 1 }
exit 0
