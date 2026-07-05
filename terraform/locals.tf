locals {
  base_name            = "${replace(var.prefix, "_", "-")}-${random_string.main.result}"
  base_name_nodash     = replace(local.base_name, "-", "")
  container_app_domain = azurerm_container_app_environment.main.default_domain
  front_service_url    = "https://ca-frontservice-${local.base_name}.${local.container_app_domain}"
  sse_service_url      = "https://ca-sseservice-${local.base_name}.${local.container_app_domain}"
  history_api_url      = "https://ca-historyapi-${local.base_name}.${local.container_app_domain}"
  memory_api_url       = "https://ca-memoryapi-${local.base_name}.${local.container_app_domain}"
  web_client_url       = "https://ca-webclient-${local.base_name}.${local.container_app_domain}"
  agent_client_url     = "https://ca-agentclient-${local.base_name}.${local.container_app_domain}"
  storage_account_url  = "https://${azapi_resource.storage_account_main.name}.blob.core.windows.net"
}
