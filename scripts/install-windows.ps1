param(
  [string]$Python = "py"
)

$ErrorActionPreference = "Stop"

function Ask([string]$Prompt, [string]$Default = "") {
  if ($Default) {
    $value = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
  }
  return (Read-Host $Prompt).Trim()
}

function Ask-Secret([string]$Prompt) {
  $secure = Read-Host $Prompt -AsSecureString
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
    return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  } finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
  }
}

function Env-Value([string]$Value) {
  if ($null -eq $Value) { return "" }
  if ($Value -match '^[A-Za-z0-9_./:@*+\-=]*$') { return $Value }
  $escaped = $Value.Replace("\", "\\").Replace('"', '\"')
  return '"' + $escaped + '"'
}

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

Write-Host "== XyraNet Reseller Autoseller Windows installer =="
Write-Host "This installer is intended for local testing on Windows."
Write-Host

if (Test-Path ".env") {
  $overwrite = Ask "A .env file already exists. Overwrite it? yes/no" "no"
  if ($overwrite -ne "yes" -and $overwrite -ne "y") {
    Write-Host ".env was kept unchanged."
    if (-not (Test-Path ".venv")) {
      & $Python -m venv .venv
    }
    & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
    & ".\.venv\Scripts\pip.exe" install -r requirements.txt
    Write-Host
    Write-Host "Installed dependencies. Start with:"
    Write-Host ".\.venv\Scripts\python.exe run.py"
    exit 0
  }
}

$adminUsername = Ask "Web panel login" "admin"
$adminPassword = Ask-Secret "Web panel password"
while ($adminPassword.Length -lt 8) {
  $adminPassword = Ask-Secret "Web panel password must be at least 8 characters"
}

$panelLanguage = (Ask "Interface and Telegram bot language: ru/en" "ru").ToLowerInvariant()
while ($panelLanguage -ne "ru" -and $panelLanguage -ne "en") {
  $panelLanguage = (Ask "Language must be ru or en" "ru").ToLowerInvariant()
}

$telegramBotToken = Ask-Secret "Telegram bot token (can be empty)"
$adminIds = Ask "Telegram admin ID, comma separated" ""
while ([string]::IsNullOrWhiteSpace($adminIds)) {
  $adminIds = Ask "Telegram admin ID is required"
}

$xyranetApiKey = Ask-Secret "XyraNet API key (can be filled later in panel)"
$digisellerSellerId = Ask "Digiseller seller ID (optional)" ""
$digisellerApiKey = Ask-Secret "Digiseller API key (optional)"
$ggselSellerId = Ask "GGsel seller ID (optional)" ""
$ggselApiKey = Ask-Secret "GGsel API key (optional)"

@(
  "APP_HOST=127.0.0.1",
  "APP_PORT=8095",
  "APP_BASE_URL=http://127.0.0.1:8095",
  "",
  "XYRANET_API_BASE_URL=https://xyranet.pro/api/wholesale",
  "XYRANET_API_KEY=$(Env-Value $xyranetApiKey)",
  "XYRANET_TIMEOUT_SECONDS=30",
  "",
  "DIGISELLER_SELLER_ID=$(Env-Value $digisellerSellerId)",
  "DIGISELLER_API_KEY=$(Env-Value $digisellerApiKey)",
  "GGSEL_SELLER_ID=$(Env-Value $ggselSellerId)",
  "GGSEL_API_KEY=$(Env-Value $ggselApiKey)",
  "",
  "TELEGRAM_BOT_TOKEN=$(Env-Value $telegramBotToken)",
  "ADMIN_IDS=$(Env-Value $adminIds)",
  "ADMIN_USERNAME=$(Env-Value $adminUsername)",
  "ADMIN_PASSWORD=$(Env-Value $adminPassword)",
  "",
  "DATABASE_PATH=data/reseller.sqlite3",
  "PANEL_LANGUAGE=$panelLanguage",
  "ENABLE_TELEGRAM=true",
  "LOG_LEVEL=INFO"
) | Set-Content -Encoding UTF8 ".env"

if (-not (Test-Path ".venv")) {
  & $Python -m venv .venv
}
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\pip.exe" install -r requirements.txt

Write-Host
Write-Host "Installed."
Write-Host "Start the panel with:"
Write-Host ".\.venv\Scripts\python.exe run.py"
Write-Host "Then open: http://127.0.0.1:8095"
