# Arranca el backend de Bonsai en local, sin Docker (Windows / PowerShell).
#
#   .\run-local.ps1
#
# Crea el entorno virtual la primera vez, instala las dependencias, carga el
# .env y levanta el servidor en http://127.0.0.1:8080 con recarga automatica.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "He creado el fichero .env a partir de .env.example." -ForegroundColor Yellow
    Write-Host "Abrelo, pon tu GEMINI_API_KEY y vuelve a ejecutar este script." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creando el entorno virtual (.venv)..." -ForegroundColor Cyan
    python -m venv .venv
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "Instalando dependencias..." -ForegroundColor Cyan
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements.txt

# Carga el .env en las variables de entorno de este proceso.
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $name = $matches[1]
        $value = $matches[2].Trim().Trim('"')
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

# Solo se avisa de la clave del proveedor que se vaya a usar de verdad.
$proveedor = if ($env:VISION_PROVIDER) { $env:VISION_PROVIDER } else { "gemini" }
if ($proveedor -eq "groq") {
    $clave = $env:GROQ_API_KEY; $esperado = "tu-api-key-de-groq"; $nombre = "GROQ_API_KEY"
} else {
    $clave = $env:GEMINI_API_KEY; $esperado = "tu-api-key-de-gemini"; $nombre = "GEMINI_API_KEY"
}
if (-not $clave -or $clave -eq $esperado) {
    Write-Host ""
    Write-Host "Falta $nombre en el .env: /describe y /look daran error 500." -ForegroundColor Yellow
    Write-Host "El resto (/speak, /memory, /voices, /probar) si funcionara:" -ForegroundColor Yellow
    Write-Host "la voz es Piper y va en local, sin ninguna clave." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Servidor:      http://127.0.0.1:8080" -ForegroundColor Green
Write-Host "  Documentacion: http://127.0.0.1:8080/docs" -ForegroundColor Green
Write-Host "  Desde el movil: http://127.0.0.1:8080/probar" -ForegroundColor Green
Write-Host "  Para probarlo: python test_bonsai.py  (en otra terminal)" -ForegroundColor Green
Write-Host "  Ctrl+C para parar."
Write-Host ""

& $py -m uvicorn main:app --host 127.0.0.1 --port 8080 --reload
