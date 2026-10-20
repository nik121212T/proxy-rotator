param([switch]$InstallPython, [switch]$CheckOnly, [switch]$PrepareOnly)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$project = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $project
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$managed = Join-Path $env:LOCALAPPDATA 'Programs\ProxyRotatorPython313'
$probe = 'import sys,struct,platform; assert (3,11)<=sys.version_info<(3,14) and struct.calcsize(''P'')==8 and platform.machine().lower() in (''amd64'',''x86_64''); print(sys.executable)'

function Test-Python([string]$Executable) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $null }
    if ($Executable -like '*\Microsoft\WindowsApps\*') { return $null }
    try {
        $result = & $Executable -c $probe 2>$null
        if ($LASTEXITCODE -eq 0 -and $result) { return [string]($result | Select-Object -Last 1) }
    } catch { }
    return $null
}

function Find-Python {
    $candidates = @((Join-Path $project '.venv\Scripts\python.exe'), (Join-Path $managed 'python.exe'))
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @('-3.13','-3.12','-3.11')) {
            try {
                $result = & $launcher.Source $version -c $probe 2>$null
                if ($LASTEXITCODE -eq 0 -and $result) { $candidates += [string]($result | Select-Object -Last 1) }
            } catch { }
        }
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    foreach ($version in @('313','312','311')) {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"
    }
    foreach ($candidate in $candidates) {
        $found = Test-Python $candidate
        if ($found) { return $found }
    }
    return $null
}

try {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        throw 'Windows x64 Intel/AMD est requis pour cette distribution.'
    }
    Write-Host '[1/6] Recherche de Python 3.11 a 3.13 x64...'
    $python = Find-Python
    if ($InstallPython -or -not $python) {
        $version = '3.13.16'
        $uri = "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
        $temporary = Join-Path ([IO.Path]::GetTempPath()) ('proxy-rotator-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $temporary | Out-Null
        try {
            $installer = Join-Path $temporary 'python-installer.exe'
            Write-Host "[1/6] Telechargement de Python $version depuis python.org..."
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -Uri $uri -OutFile $installer -TimeoutSec 180
            Write-Host '[1/6] Verification de la signature de Python Software Foundation...'
            $signature = Get-AuthenticodeSignature -LiteralPath $installer
            if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or
                $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
                throw 'Signature Python invalide : fichier non execute.'
            }
            Write-Host '[1/6] Installation de Python pour cet utilisateur (patienter)...'
            $arguments = @('/quiet', 'InstallAllUsers=0', ('TargetDir="{0}"' -f $managed),
                'PrependPath=0', 'AssociateFiles=0', 'Shortcuts=0', 'Include_launcher=0',
                'Include_test=0', 'Include_doc=0', 'Include_tcltk=0', 'Include_pip=1')
            $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
            if ($process.ExitCode -eq 3010) { throw 'Python installe. Redemarrez Windows puis relancez Lancer.bat.' }
            if ($process.ExitCode -ne 0) { throw "Installation Python echouee (code $($process.ExitCode))." }
            $python = Test-Python (Join-Path $managed 'python.exe')
            if (-not $python) { $python = Find-Python }
            if (-not $python) { throw 'Python installe mais introuvable ou incompatible.' }
        } finally {
            Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host '[1/6] Python x64 compatible disponible.'
    if ($CheckOnly) { exit 0 }
    $bootstrap = Join-Path $PSScriptRoot 'windows_bootstrap.py'
    if ($PrepareOnly) { & $python -u $bootstrap --prepare-only }
    else { & $python -u $bootstrap }
    exit $LASTEXITCODE
} catch {
    Write-Host ('[ERREUR] ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Aucune donnee du projet supprimee. Corrigez le probleme puis relancez Lancer.bat.'
    exit 1
}
