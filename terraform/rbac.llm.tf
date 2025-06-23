resource "azurerm_role_assignment" "llm_worker_llm" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Cognitive Services OpenAI Contributor"
  principal_id         = azurerm_user_assigned_identity.llm_worker.principal_id
}

resource "azurerm_role_assignment" "history_worker_llm" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.history_worker.principal_id
}

resource "azurerm_role_assignment" "memory_worker_llm" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.memory_worker.principal_id
}

resource "azurerm_role_assignment" "memory_api_llm" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.memory_api.principal_id
}
