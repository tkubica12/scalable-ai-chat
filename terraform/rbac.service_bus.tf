resource "azurerm_role_assignment" "self_servicebus_sender" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "front_service_servicebus_sender" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_user_assigned_identity.front_service.principal_id
}

# resource "azurerm_role_assignment" "app_servicebus_sender" {
#   scope                = azurerm_servicebus_namespace.main.id
#   role_definition_name = "Azure Service Bus Data Sender"
#   principal_id         = azurerm_user_assigned_identity.main.principal_id
# }

resource "azurerm_role_assignment" "self_servicebus_receiver" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "front_service_servicebus_receiver" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.front_service.principal_id
}

# resource "azurerm_role_assignment" "app_servicebus_receiver" {
#   scope                = azurerm_servicebus_namespace.main.id
#   role_definition_name = "Azure Service Bus Data Receiver"
#   principal_id         = azurerm_user_assigned_identity.main.principal_id
# }

resource "azurerm_role_assignment" "llm_worker_servicebus_receiver" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.llm_worker.principal_id
}

resource "azurerm_role_assignment" "llm_worker_servicebus_sender" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_user_assigned_identity.llm_worker.principal_id
}

resource "azurerm_role_assignment" "sse_service_servicebus_receiver" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.sse_service.principal_id
}

resource "azurerm_role_assignment" "history_worker_servicebus_receiver" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.history_worker.principal_id
}

resource "azurerm_role_assignment" "memory_worker_servicebus_receiver" {
  scope                = azurerm_servicebus_namespace.main.id
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.memory_worker.principal_id
}
