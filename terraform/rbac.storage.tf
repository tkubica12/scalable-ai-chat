resource "azurerm_role_assignment" "self_storage" {
  scope                = azapi_resource.storage_account_main.id
  role_definition_name = "Storage Blob Data Owner"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "front_service_storage" {
  scope                = azapi_resource.storage_account_main.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.front_service.principal_id
}

resource "azurerm_role_assignment" "llm_worker_storage" {
  scope                = azapi_resource.storage_account_main.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.llm_worker.principal_id
}