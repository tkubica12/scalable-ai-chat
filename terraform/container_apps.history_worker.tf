resource "azapi_resource" "history_worker" {
  type      = "Microsoft.App/containerApps@2025-01-01"
  name      = "ca-historyworker-${local.base_name}"
  location  = azurerm_resource_group.main.location
  parent_id = azurerm_resource_group.main.id

  body = {
    identity = {
      type = "UserAssigned"
      userAssignedIdentities = {
        "${azurerm_user_assigned_identity.history_worker.id}" = {}
      }
    }
    properties = {
      managedEnvironmentId = azurerm_container_app_environment.main.id
      configuration = {
        activeRevisionsMode = "Single"
      }
      template = {
        containers = [
          {
            name  = "history-worker"
            image = "ghcr.io/${var.github_repository}/history-worker:latest"
            resources = {
              cpu    = 0.25
              memory = "0.5Gi"
            }
            env = [
              {
                name  = "AZURE_CLIENT_ID"
                value = azurerm_user_assigned_identity.history_worker.client_id
              },
              {
                name  = "SERVICEBUS_FULLY_QUALIFIED_NAMESPACE"
                value = "${azurerm_servicebus_namespace.main.name}.servicebus.windows.net"
              },
              {
                name  = "SERVICEBUS_MESSAGE_COMPLETED_TOPIC"
                value = azurerm_servicebus_topic.message_completed.name
              },
              {
                name  = "SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION"
                value = azurerm_servicebus_subscription.history_worker_message_completed.name
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
                }, {
                name  = "REDIS_HOST"
                value = azapi_resource.redis.output.properties.hostName
              },
              {
                name  = "REDIS_PORT"
                value = "10000"
              },
              {
                name  = "REDIS_SSL"
                value = "true"
              },
              {
                name  = "MAX_CONCURRENCY"
                value = "10"
              },
              {
                name  = "LOG_LEVEL"
                value = "INFO"
              },
              {
                name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
                value = azurerm_application_insights.main.connection_string
                }, {
                name  = "OTEL_SERVICE_NAME"
                value = "history-worker"
              },
              {
                name  = "AZURE_OPENAI_ENDPOINT"
                value = "https://${azapi_resource.ai_service.name}.openai.azure.com"
              },
              {
                name  = "AZURE_OPENAI_DEPLOYMENT_NAME"
                value = azurerm_cognitive_deployment.openai_model.name
              },
              {
                name  = "AZURE_OPENAI_API_VERSION"
                value = "2025-04-01-preview"
              }
            ]
          }
        ]
        scale = {
          cooldownPeriod = var.container_app_cooldown_period
          minReplicas    = var.container_app_min_replicas
          maxReplicas    = 5
          rules = [
            {
              name = "service-bus-topic-scale-rule"
              custom = {
                type = "azure-servicebus"
                metadata = {
                  topicName        = azurerm_servicebus_topic.message_completed.name
                  subscriptionName = azurerm_servicebus_subscription.history_worker_message_completed.name
                  namespace        = azurerm_servicebus_namespace.main.name
                  messageCount     = "5"
                },
                identity = azurerm_user_assigned_identity.history_worker.id
              }
            }
          ]
        }
        # Configure graceful shutdown with 1-minute grace period for history processing
        terminationGracePeriodSeconds = 60
      }
    }
  }
}
