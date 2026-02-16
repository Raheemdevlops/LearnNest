# run.ps1 - Create venv, install deps, copy .env.example -> .env, run app
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Ensure python is available
try {
    python --version | Out-Null
} catch {
    Write-Error "Python is not on PATH. Install Python 3.9+ and re-run."
    exit 1
}

$projectRoot = $PSScriptRoot
$venvPath = Join-Path $projectRoot ".venv"

# Create virtual environment if missing
if (-not (Test-Path $venvPath)) {
    Write-Host "Creating virtual environment at $venvPath..."
    python -m venv $venvPath
}

# Activate venv for this script
$activate = Join-Path $venvPath "Scripts\Activate.ps1"
if (Test-Path $activate) {
    Write-Host "Activating virtual environment..."
    . $activate
} else {
    Write-Error "Activation script not found at $activate"
    exit 1
}

# Upgrade pip and install requirements
Write-Host "Upgrading pip and installing dependencies..."
python -m pip install --upgrade pip
$req = Join-Path $projectRoot "requirements.txt"
if (Test-Path $req) {
    pip install -r $req
} else {
    Write-Warning "requirements.txt not found in project root."
}

# Create .env from example if missing
$envFile = Join-Path $projectRoot ".env"
$envExample = Join-Path $projectRoot ".env.example"
if (-not (Test-Path $envFile)) {
    if (Test-Path $envExample) {
        Copy-Item -Path $envExample -Destination $envFile
        Write-Host "Created .env from .env.example. Please edit .env to add secrets."
    } else {
        Write-Host "Creating .env with placeholder values..."
        @"
SESSION_SECRET=change-me
GEMINI_API_KEY=
MAIL_SERVER=
MAIL_PORT=
MAIL_USERNAME=
MAIL_PASSWORD=
"@ | Out-File -Encoding utf8 $envFile
        Write-Host "Created .env — edit it with real values before using production features."
    }
} else {
    Write-Host ".env already exists. Skipping creation."
}

# Run the app
Write-Host "Starting LearnNest app (python app.py)..."
python app.py
