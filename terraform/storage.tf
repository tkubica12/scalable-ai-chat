resource "azapi_resource" "storage_account_main" {
  type      = "Microsoft.Storage/storageAccounts@2024-01-01"
  name      = "st${local.base_name_nodash}"
  parent_id = azurerm_resource_group.main.id
  location  = azurerm_resource_group.main.location

  body = {
    sku = {
      name = "Standard_LRS"
    }
    kind = "StorageV2"
    properties = {
      defaultToOAuthAuthentication = true
      isLocalUserEnabled           = false
      supportsHttpsTrafficOnly     = true
      minimumTlsVersion            = "TLS1_2"
      allowBlobPublicAccess        = false
    }
  }
}

# Private Blob container for durable artifact storage (charts, tables, files, generated
# HTML/JS artifacts produced by chat runs). Access is only via Entra ID/managed identity
# (Storage Blob Data Contributor/Reader RBAC) or short-lived user-delegation SAS URLs minted
# by the artifact API; the container itself stays private (no public/anonymous access).
resource "azurerm_storage_container" "artifacts" {
  name                  = "artifacts"
  storage_account_id    = azapi_resource.storage_account_main.id
  container_access_type = "private"
}
