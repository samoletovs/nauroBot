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

@description('Salt appended to the app name to force a fresh instance when recovering from a wedged one. The live app is naurobot-func-<suffix>b.')
param instanceId string = 'b'

// The Flex plan and package container were auto-named when the app moved off Linux
// Consumption (2026-10). A Flex app cannot change plans, so these must match the live names.
// A Flex plan hosts exactly one app: a recovery with a new instanceId must also pass a
// fresh functionPlanName and deploymentContainerName.
@description('Flex Consumption plan name')
param functionPlanName string = 'ASP-naurobotrg-302e'

@description('Blob container holding the Flex deployment package')
param deploymentContainerName string = 'app-package-naurobotfunctu72uwipcdj2sb-9005431'

@description('True when the Function App already exists. Its live app settings are then merged under the declared ones, so secrets set out of band survive a redeploy. Pass false only when creating the app.')
param appExists bool = true

var suffix = uniqueString(resourceGroup().id)
var functionAppName = '${projectName}-func-${suffix}${instanceId}'
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

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainerName
  properties: { publicAccess: 'None' }
}

var storageConnection = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'

// siteConfig.appSettings replaces the whole set, so read the live settings first and let the
// declared values win. Blank secret params are dropped rather than written, so a plain
// redeploy keeps the live secrets instead of erasing them.
var declaredAppSettings = filter([
  { name: 'AzureWebJobsStorage', value: storageConnection }
  { name: 'DEPLOYMENT_STORAGE_CONNECTION_STRING', value: storageConnection }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: monitoring.outputs.connectionString }
  { name: 'NAURO_BOT_TOKEN', value: botToken }
  { name: 'TELEGRAM_WEBHOOK_SECRET', value: webhookSecret }
  { name: 'GH_ASSIGN_PAT', value: githubPat }
  { name: 'NAURO_CHAT_ID', value: allowedChatId }
  { name: 'NAURO_GITHUB_OWNER', value: githubOwner }
], s => !empty(s.value))
var liveAppSettings = liveSettings.outputs.settings
var appSettings = map(items(union(liveAppSettings, toObject(declaredAppSettings, s => s.name, s => s.value))), s => { name: s.key, value: s.value })

module liveSettings 'modules/live-app-settings.bicep' = {
  name: 'live-app-settings-${projectName}'
  params: {
    functionAppName: functionAppName
    appExists: appExists
  }
}

// ── Function App (Flex Consumption, Python 3.11). Stateless webhook, so the
//    instance's ephemeral disk is fine. ──
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: functionPlanName
  location: location
  tags: tags
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  kind: 'functionapp'
  properties: { reserved: true }
}

resource func 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  dependsOn: [ deploymentContainer ]
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'StorageAccountConnectionString'
            storageAccountConnectionStringName: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
          }
        }
      }
      scaleAndConcurrency: { maximumInstanceCount: 10, instanceMemoryMB: 2048 }
      runtime: { name: 'python', version: '3.11' }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: appSettings
    }
  }
}

output functionAppName string = func.name
output webhookUrl string = 'https://${func.name}.azurewebsites.net/api/telegram'
output storageAccountName string = storage.name
output appInsightsConnectionString string = monitoring.outputs.connectionString
