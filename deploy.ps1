# nauroBot deploy — provisions infra (Function App + storage + monitoring) and publishes
# the webhook. Run from the repo root: pwsh ./deploy.ps1
#
# The GitHub issue is the state store for the feedback loop, so the Function App runs on the
# cheap Y1 Consumption plan. Secret app settings are set once, after the first deploy; then
# the Telegram webhook is registered so taps/replies reach the endpoint.
param(
    [string]$ResourceGroup = "naurobot-rg",
    [string]$Location = "northeurope"
)
$ErrorActionPreference = "Stop"

Write-Host "== Tests ==" -ForegroundColor Cyan
Push-Location $PSScriptRoot
python -m unittest discover tests
if ($LASTEXITCODE -ne 0) { Write-Error "tests failed — aborting"; exit 1 }
Pop-Location

Write-Host "== Infrastructure (Bicep) ==" -ForegroundColor Cyan
az group create -n $ResourceGroup -l $Location -o none
$outputs = az deployment group create -g $ResourceGroup -n naurobot-deploy `
    --template-file "$PSScriptRoot/infrastructure/main.bicep" `
    --query "properties.outputs" -o json | ConvertFrom-Json
$funcApp = $outputs.functionAppName.value
$webhookUrl = $outputs.webhookUrl.value
Write-Host "Function App: $funcApp" -ForegroundColor Green
Write-Host "Webhook URL:  $webhookUrl" -ForegroundColor Green

Write-Host "== Publish function code (remote build) ==" -ForegroundColor Cyan
Push-Location "$PSScriptRoot/functions"
func azure functionapp publish $funcApp --build remote --python
Pop-Location

Write-Host ""
Write-Host "== Next: one-time secrets + webhook registration ==" -ForegroundColor Yellow
Write-Host "1. Set the secret app settings (values NOT in Bicep):"
Write-Host "   az functionapp config appsettings set -g $ResourceGroup -n $funcApp --settings ``"
Write-Host '     NAURO_BOT_TOKEN=<ops bot token>  TELEGRAM_WEBHOOK_SECRET=<random secret> `'
Write-Host '     GH_ASSIGN_PAT=<user PAT, repo scope>  NAURO_CHAT_ID=<your chat id>'
Write-Host ""
Write-Host "2. Register the Telegram webhook (Telegram echoes the secret back in a header):"
Write-Host "   Invoke-RestMethod -Method Post ``"
Write-Host "     -Uri `"https://api.telegram.org/bot<NAURO_BOT_TOKEN>/setWebhook`" ``"
Write-Host "     -Body @{ url = `"$webhookUrl`"; secret_token = `"<TELEGRAM_WEBHOOK_SECRET>`";"
Write-Host "             allowed_updates = '[`"callback_query`",`"message`"]' }"
