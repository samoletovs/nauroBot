targetScope = 'resourceGroup'

@description('Project name (lowerCamelCase; flatcase for resource names)')
param projectName string = 'naurobot'

@description('Azure region')
param location string = 'northeurope'

@description('NauroLabs ops bot token (shared with autoRefine, which sends the idea cards)')
@secure()
param botToken string = ''

@description('Secret token registered with Telegram setWebhook; verified on every request')
@secure()
param webhookSecret string = ''

@description('User PAT (repo scope) to relabel/close issues and assign the Copilot agent')
@secure()
param githubPat string = ''

@description('Telegram chat id allowed to drive the bot (empty = allow any)')
param allowedChatId string = ''

@description('GitHub owner for NauroLabs project repos')
param githubOwner string = 'samoletovs'

@description('Salt appended to plan/app names to force a fresh instance when recovering from a wedged one')
param instanceId string = ''

var suffix = uniqueString(resourceGroup().id)
var tags = {
  project: projectName
  managedBy: 'bicep'
  costCenter: 'naurolabs-research'
}

// Shared monitoring module (App Insights + Log Analytics) — golden path.
module monitoring '../../.github/infrastructure/modules/monitoring.bicep' = {
  name: 'monitoring-${projectName}'
  params: {
    projectName: projectName
    location: location
    tags: tags
  }
}

// ── Storage (required by the Function App runtime; no app state lives here — the
//    GitHub issue is the state store for the feedback loop) ──
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${projectName}${suffix}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
  }
}

// ── Function App (Linux Consumption, Python 3.11). Stateless webhook, so the plan's
//    ephemeral disk is fine. ──
resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${projectName}-plan-${suffix}${instanceId}'
  location: location
  tags: tags
  sku: { name: 'Y1', tier: 'Dynamic' }
  kind: 'functionapp'
  properties: { reserved: true }
}

resource func 'Microsoft.Web/sites@2023-12-01' = {
  name: '${projectName}-func-${suffix}${instanceId}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    reserved: true
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage', value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}' }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: monitoring.outputs.connectionString }
        { name: 'NAURO_BOT_TOKEN', value: botToken }
        { name: 'TELEGRAM_WEBHOOK_SECRET', value: webhookSecret }
        { name: 'GH_ASSIGN_PAT', value: githubPat }
        { name: 'NAURO_CHAT_ID', value: allowedChatId }
        { name: 'NAURO_GITHUB_OWNER', value: githubOwner }
      ]
    }
  }
}

output functionAppName string = func.name
output webhookUrl string = 'https://${func.name}.azurewebsites.net/api/telegram'
output storageAccountName string = storage.name
output appInsightsConnectionString string = monitoring.outputs.connectionString
