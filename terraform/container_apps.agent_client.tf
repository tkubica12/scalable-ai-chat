resource "azapi_resource" "agent_client" {
  type      = "Microsoft.App/containerApps@2025-01-01"
  name      = "ca-agentclient-${local.base_name}"
  location  = azurerm_resource_group.main.location
  parent_id = azurerm_resource_group.main.id

  body = {
    properties = {
      managedEnvironmentId = azurerm_container_app_environment.main.id
      configuration = {
        activeRevisionsMode = "Single"
        ingress = {
          external   = true
          targetPort = 3000
          transport  = "Http"
          traffic = [
            {
              latestRevision = true
              weight         = 100
            }
          ]
        }
      }
      template = {
        scale = {
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
            name  = "agent-client"
            image = "ghcr.io/${var.github_repository}/agent-client:latest"
            resources = {
              cpu    = 0.5
              memory = "1Gi"
            }
            env = [
              {
                name  = "NEXT_PUBLIC_API_URL"
                value = local.front_service_url
              },
              {
                name  = "NEXT_PUBLIC_SSE_URL"
                value = local.sse_service_url
              }
            ]
          }
        ]
      }
    }
  }
}
