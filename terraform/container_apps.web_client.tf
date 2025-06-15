resource "azapi_resource" "web_client" {
  type      = "Microsoft.App/containerApps@2025-01-01"
  name      = "ca-webclient-${local.base_name}"
  location  = azurerm_resource_group.main.location
  parent_id = azurerm_resource_group.main.id

  body = {
    properties = {
      managedEnvironmentId = azurerm_container_app_environment.main.id
      configuration = {
        activeRevisionsMode = "Single"
        ingress = {
          external   = true
          targetPort = 80
          transport  = "Http"
          traffic = [
            {
              latestRevision = true
              weight         = 100
            }
          ]
        }
      }
      template = { scale = {
        cooldownPeriod = var.container_app_cooldown_period
        minReplicas    = var.container_app_min_replicas
        maxReplicas    = 3
        rules = [
          {
            name = "http-scale-rule"
            http = {
              metadata = {
                concurrentRequests = "50"
              }
            }
          }
        ]
        }
        containers = [
          {
            name  = "web-client"
            image = "ghcr.io/${var.github_repository}/web-client:latest"
            resources = {
              cpu    = 0.25
              memory = "0.5Gi"
            }
            env = [
              {
                name  = "API_URL"
                value = "https://${azapi_resource.front_service.output.properties.configuration.ingress.fqdn}"
              },
              {
                name  = "SSE_URL"
                value = "https://${azapi_resource.sse_service.output.properties.configuration.ingress.fqdn}"
              },
              {
                name  = "HISTORY_API_URL"
                value = "https://${azapi_resource.history_api.output.properties.configuration.ingress.fqdn}"
              },
              {
                name  = "MEMORY_API_URL"
                value = "https://${azapi_resource.memory_api.output.properties.configuration.ingress.fqdn}"
              },
            ]
          }
        ]
      }
    }
  }
}
