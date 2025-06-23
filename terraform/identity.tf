# User-assigned managed identities for each container app
resource "azurerm_user_assigned_identity" "front_service" {
  name                = "${local.base_name}-front-service-identity"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_user_assigned_identity" "history_api" {
  name                = "${local.base_name}-history-api-identity"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_user_assigned_identity" "history_worker" {
  name                = "${local.base_name}-history-worker-identity"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_user_assigned_identity" "llm_worker" {
  name                = "${local.base_name}-llm-worker-identity"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_user_assigned_identity" "memory_api" {
  name                = "${local.base_name}-memory-api-identity"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_user_assigned_identity" "memory_worker" {
  name                = "${local.base_name}-memory-worker-identity"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_user_assigned_identity" "sse_service" {
  name                = "${local.base_name}-sse-service-identity"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

# # Legacy identity - keep for backward compatibility or remove if not needed elsewhere
# resource "azurerm_user_assigned_identity" "main" {
#   name                = "${local.base_name}-identity"
#   location            = azurerm_resource_group.main.location
#   resource_group_name = azurerm_resource_group.main.name
# }