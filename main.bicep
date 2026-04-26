// ============================================================================
// Infraestrutura como Código — RAG TEA Conhecimento
// Provisiona: Storage Account, Azure OpenAI, AI Search, Function App
//
// Deploy:
//   az deployment sub create \
//     --location eastus2 \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters.json
// ============================================================================

targetScope = 'subscription'

@description('Prefixo para nomes dos recursos (ex: tearag)')
param prefix string = 'tearag'

@description('Região dos recursos')
param location string = 'eastus2'

@description('Nome do Resource Group')
param resourceGroupName string = '${prefix}-rg'

// ─── Resource Group ──────────────────────────────────────────────────────────
resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
}

var suffix = uniqueString(rg.id)
var storageAccountName = '${prefix}st${suffix}'
var functionAppName = '${prefix}-func-${suffix}'
var openaiName = '${prefix}-openai-${suffix}'
var searchName = '${prefix}-search-${suffix}'
var appServicePlanName = '${prefix}-plan-${suffix}'

// ─── Storage Account ─────────────────────────────────────────────────────────
resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: take(storageAccountName, 24)
  location: location
  scope: rg
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

var storageConnStr = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=core.windows.net'

// ─── Azure OpenAI ─────────────────────────────────────────────────────────────
resource openai 'Microsoft.CognitiveServices/accounts@2023-10-01-preview' = {
  name: openaiName
  location: location
  scope: rg
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    publicNetworkAccess: 'Enabled'
    customSubDomainName: openaiName
  }
}

resource embeddingDeploy 'Microsoft.CognitiveServices/accounts/deployments@2023-10-01-preview' = {
  name: 'text-embedding-3-small'
  parent: openai
  sku: { name: 'Standard', capacity: 120 }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-small'
      version: '1'
    }
  }
}

resource chatDeploy 'Microsoft.CognitiveServices/accounts/deployments@2023-10-01-preview' = {
  name: 'gpt-4o'
  parent: openai
  dependsOn: [embeddingDeploy]
  sku: { name: 'Standard', capacity: 80 }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4o'
      version: '2024-05-13'
    }
  }
}

// ─── Azure AI Search ──────────────────────────────────────────────────────────
resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: searchName
  location: location
  scope: rg
  sku: { name: 'free' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    publicNetworkAccess: 'Enabled'
  }
}

// ─── App Service Plan (Consumption) ──────────────────────────────────────────
resource plan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: appServicePlanName
  location: location
  scope: rg
  sku: { name: 'Y1', tier: 'Dynamic' }
  kind: 'functionapp'
  properties: { reserved: true }
}

// ─── Function App ─────────────────────────────────────────────────────────────
resource funcApp 'Microsoft.Web/sites@2023-01-01' = {
  name: functionAppName
  location: location
  scope: rg
  kind: 'functionapp,linux'
  properties: {
    serverFarmId: plan.id
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      appSettings: [
        { name: 'AzureWebJobsStorage', value: storageConnStr }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'AZURE_OPENAI_ENDPOINT', value: openai.properties.endpoint }
        { name: 'AZURE_OPENAI_KEY', value: openai.listKeys().key1 }
        { name: 'AZURE_OPENAI_CHAT_DEPLOYMENT', value: 'gpt-4o' }
        { name: 'AZURE_OPENAI_EMBEDDING_DEPLOYMENT', value: 'text-embedding-3-small' }
        { name: 'AZURE_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
        { name: 'AZURE_SEARCH_KEY', value: search.listAdminKeys().primaryKey }
        { name: 'AZURE_SEARCH_INDEX_NAME', value: 'tea-conhecimento' }
        { name: 'CHUNK_SIZE', value: '800' }
        { name: 'CHUNK_OVERLAP', value: '100' }
        { name: 'RAG_TOP_N', value: '5' }
      ]
    }
    httpsOnly: true
  }
}

// ─── Outputs ─────────────────────────────────────────────────────────────────
output resourceGroupName string = rg.name
output functionAppName string = funcApp.name
output functionAppUrl string = 'https://${funcApp.properties.defaultHostName}'
output openaiEndpoint string = openai.properties.endpoint
output searchEndpoint string = 'https://${search.name}.search.windows.net'

#disable-next-line outputs-should-not-contain-secrets
output openaiKey string = openai.listKeys().key1
#disable-next-line outputs-should-not-contain-secrets
output searchKey string = search.listAdminKeys().primaryKey
