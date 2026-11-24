// MomentLens - virtual network and private DNS zones.
//
// The web app and the vi-poller job reach the storage account through private endpoints, so their traffic to
// blob and table storage stays on the virtual network.
//   snet-app  App Service regional VNet integration
//   snet-cae  Container Apps environment (workload profiles)
//   snet-pe   private endpoints
targetScope = 'resourceGroup'

@description('Azure region.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('Name of the virtual network.')
param vnetName string

resource vnet 'Microsoft.Network/virtualNetworks@2025-07-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.40.0.0/16'] }
    subnets: [
      {
        name: 'snet-app'
        properties: {
          addressPrefix: '10.40.0.0/26'
          delegations: [
            { name: 'Microsoft.Web.serverFarms', properties: { serviceName: 'Microsoft.Web/serverFarms' } }
          ]
        }
      }
      {
        name: 'snet-cae'
        properties: {
          addressPrefix: '10.40.4.0/23'
          delegations: [
            { name: 'Microsoft.App.environments', properties: { serviceName: 'Microsoft.App/environments' } }
          ]
        }
      }
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: '10.40.8.0/26'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource snetApp 'Microsoft.Network/virtualNetworks/subnets@2025-07-01' existing = {
  parent: vnet
  name: 'snet-app'
}

resource snetCae 'Microsoft.Network/virtualNetworks/subnets@2025-07-01' existing = {
  parent: vnet
  name: 'snet-cae'
}

resource snetPe 'Microsoft.Network/virtualNetworks/subnets@2025-07-01' existing = {
  parent: vnet
  name: 'snet-pe'
}

resource dnsBlob 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource dnsBlobLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: dnsBlob
  name: 'link-blob'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

resource dnsTable 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.table.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource dnsTableLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: dnsTable
  name: 'link-table'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

output appSubnetId string = snetApp.id
output containerEnvSubnetId string = snetCae.id
output privateEndpointSubnetId string = snetPe.id
output blobDnsZoneId string = dnsBlob.id
output tableDnsZoneId string = dnsTable.id
