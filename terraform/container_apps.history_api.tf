resource "azapi_resource" "history_api" {
  type      = "Microsoft.App/containerApps@2025-01-01"
  name      = "ca-historyapi-${local.base_name}"
  location  = azurerm_resource_group.main.location
  parent_id = azurerm_resource_group.main.id

  body = {
    identity = {
      type = "UserAssigned"
      userAssignedIdentities = {
        "${azurerm_user_assigned_identity.history_api.id}" = {}
      }
    }
    properties = {
      managedEnvironmentId = azurerm_container_app_environment.main.id
      configuration = {
        activeRevisionsMode = "Single"
        ingress = {
          external      = true
          targetPort    = 8005
          transport     = "http"
          allowInsecure = false
          traffic = [
            {
              weight         = 100
              latestRevision = true
            }
          ]
        }
      }
      template = {
        containers = [
          {
            name  = "history-api"
            image = "ghcr.io/${var.github_repository}/history-api:latest"
            resources = {
              cpu    = 0.25
              memory = "0.5Gi"
            }
            env = [
              {
                name  = "AZURE_CLIENT_ID"
                value = azurerm_user_assigned_identity.history_api.client_id
              },
              {
                name  = "COSMOS_ENDPOINT"
                value = azurerm_cosmosdb_account.main.endpoint
              },
              {
                name  = "COSMOS_DATABASE_NAME"
                value = azurerm_cosmosdb_sql_database.history.name
              },
              {
                name  = "COSMOS_CONTAINER_NAME"
                value = azapi_resource.history_conversations.name
              },
              {
                name  = "LOG_LEVEL"
                value = "INFO"
              },
              {
                name  = "CORS_ORIGINS"
                value = "*"
              },
              {
                name  = "PORT"
                value = "8005"
              },
              {
                name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
                value = azurerm_application_insights.main.connection_string
              },
              {
                name  = "OTEL_SERVICE_NAME"
                value = "history-api"
            }]
          }
        ]
        scale = {
          cooldownPeriod = var.container_app_cooldown_period
          minReplicas    = var.container_app_min_replicas
          maxReplicas    = 5
          rules = [
            {
              name = "http-scale-rule"
              http = {
                metadata = {
                  concurrentRequests = "10"
                }
              }
            }
          ]
        }
      }
    }
  }
}
