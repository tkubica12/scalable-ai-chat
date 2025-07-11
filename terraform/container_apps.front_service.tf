resource "azapi_resource" "front_service" {
  type      = "Microsoft.App/containerApps@2025-01-01"
  name      = "ca-frontservice-${local.base_name}"
  location  = azurerm_resource_group.main.location
  parent_id = azurerm_resource_group.main.id

  body = {
    identity = {
      type = "UserAssigned"
      userAssignedIdentities = {
        "${azurerm_user_assigned_identity.front_service.id}" = {}
      }
    }
    properties = {
      managedEnvironmentId = azurerm_container_app_environment.main.id
      configuration = {
        activeRevisionsMode = "Single"
        ingress = {
          external   = true
          targetPort = 8000
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
        maxReplicas    = 10
        rules = [
          {
            name = "http-scale-rule"
            http = {
              metadata = {
                concurrentRequests = "20"
              }
            }
          }
        ]
        }
        containers = [
          {
            name  = "front-service"
            image = "ghcr.io/${var.github_repository}/front-service:latest"
            resources = {
              cpu    = 0.5
              memory = "1Gi"
            }
            env = [
              {
                name  = "AZURE_CLIENT_ID"
                value = azurerm_user_assigned_identity.front_service.client_id
              },
              {
                name  = "SERVICEBUS_FULLY_QUALIFIED_NAMESPACE"
                value = "${azurerm_servicebus_namespace.main.name}.servicebus.windows.net"
              },
              {
                name  = "SERVICEBUS_USER_MESSAGES_TOPIC"
                value = azurerm_servicebus_topic.user_messages.name
              },
              {
                name  = "SERVICEBUS_SENDER_POOL_SIZE"
                value = "10"
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
                name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
                value = azurerm_application_insights.main.connection_string
              },
              {
                name  = "OTEL_SERVICE_NAME"
                value = "front-service"
              }
            ]
          }
        ]
      }
    }
  }
}
