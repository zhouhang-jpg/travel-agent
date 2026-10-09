[CmdletBinding()]
param(
    # Optional already-downloaded archive. If omitted, fetch the pinned npm archive.
    [string]$ArchivePath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$packageVersion = '1.0.16'
$archiveUrl = 'https://registry.npmjs.org/@fly-ai/flyai-cli/-/flyai-cli-1.0.16.tgz'
$archiveSha256 = 'AA133FE4E627DDAB07916EC3AFCA1D51D5A9AC5F8BD71A4219D5E8D8C30FDA40'
$bundleSha256 = '194A66EB84094F3D8EE20FAC0A2D6CAE10A405CD59AC100B7E7880EBC97297DA'
$projectRoot = Split-Path -Parent $PSScriptRoot
$installRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '.tools/flyai-cli'))
$archiveDestination = Join-Path $installRoot 'flyai-cli-1.0.16.tgz'
$bundleDestination = Join-Path $installRoot 'package/dist/flyai-bundle.cjs'
$tarCommand = Get-Command tar -CommandType Application -ErrorAction Stop

if (Test-Path -LiteralPath $bundleDestination) {
    $existingHash = (Get-FileHash -LiteralPath $bundleDestination -Algorithm SHA256).Hash
    if ($existingHash -ne $bundleSha256) {
        throw 'Existing FlyAI bundle differs from the pinned version. Review it before replacing it.'
    }
}

New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
if ($ArchivePath) {
    $sourceArchive = (Resolve-Path -LiteralPath $ArchivePath).Path
    if ((Get-FileHash -LiteralPath $sourceArchive -Algorithm SHA256).Hash -ne $archiveSha256) {
        throw 'Provided FlyAI archive does not match the pinned SHA256.'
    }
    if ($sourceArchive -ne $archiveDestination) {
        Copy-Item -LiteralPath $sourceArchive -Destination $archiveDestination
    }
} elseif (-not (Test-Path -LiteralPath $archiveDestination)) {
    Invoke-WebRequest -UseBasicParsing -Uri $archiveUrl -OutFile $archiveDestination
}

if ((Get-FileHash -LiteralPath $archiveDestination -Algorithm SHA256).Hash -ne $archiveSha256) {
    throw 'FlyAI archive hash mismatch. No files were extracted.'
}

# This exact, hash-verified archive contains three regular files. Check names
# before extracting, and never execute npm lifecycle scripts or package code.
$expectedFiles = @(
    'package/dist/flyai-bundle.cjs',
    'package/package.json',
    'package/README.md'
)
$actualFiles = @(& $tarCommand.Source -tzf $archiveDestination)
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the FlyAI archive.' }
if ($actualFiles.Count -ne $expectedFiles.Count) {
    throw 'Unexpected number of files in the FlyAI archive.'
}
foreach ($entry in $actualFiles) {
    if ($entry -notin $expectedFiles) { throw 'Unexpected path in the FlyAI archive.' }
}

& $tarCommand.Source -xzf $archiveDestination -C $installRoot
if ($LASTEXITCODE -ne 0) { throw 'Unable to extract the FlyAI archive.' }

$manifestPath = Join-Path $installRoot 'package/package.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.name -ne '@fly-ai/flyai-cli' -or $manifest.version -ne $packageVersion) {
    throw 'Extracted FlyAI package metadata does not match the requested package.'
}
if ((Get-FileHash -LiteralPath $bundleDestination -Algorithm SHA256).Hash -ne $bundleSha256) {
    throw 'Extracted FlyAI bundle hash mismatch.'
}

$provenance = [ordered]@{
    package = '@fly-ai/flyai-cli'
    version = $packageVersion
    source = $archiveUrl
    archive_sha256 = $archiveSha256
    bundle_sha256 = $bundleSha256
    npm_scripts_executed = $false
    supplier_requests_made = $false
}
$provenance | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $installRoot 'provenance.json') -Encoding utf8
Write-Output "Prepared @fly-ai/flyai-cli@$packageVersion"
Write-Output "CLI path: $bundleDestination"
Write-Output "State directory to configure: $(Join-Path $projectRoot 'private/flyai')"
Write-Output 'No npm scripts, supplier API requests, key configuration or bookings were executed.'
