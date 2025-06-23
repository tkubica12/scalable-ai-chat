resource "azapi_resource" "llm_worker" {
  type      = "Microsoft.App/containerApps@2025-01-01"
  name      = "ca-llmworker-${local.base_name}"
  location  = azurerm_resource_group.main.location
  parent_id = azurerm_resource_group.main.id

  body = {
    identity = {
      type = "UserAssigned"
      userAssignedIdentities = {
        "${azurerm_user_assigned_identity.llm_worker.id}" = {}
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
            name  = "llm-worker"
            image = "ghcr.io/${var.github_repository}/llm-worker:latest"
            resources = {
              cpu    = 0.5
              memory = "1Gi"
            }
            env = [
              {
                name  = "AZURE_CLIENT_ID"
                value = azurerm_user_assigned_identity.llm_worker.client_id
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
                name  = "SERVICEBUS_USER_MESSAGES_SUBSCRIPTION"
                value = azurerm_servicebus_subscription.worker_service_user_messages.name
              },
              {
                name  = "SERVICEBUS_TOKEN_STREAMS_TOPIC"
                value = azurerm_servicebus_topic.token_streams.name
              },
              {
                name  = "SERVICEBUS_MESSAGE_COMPLETED_TOPIC"
                value = azurerm_servicebus_topic.message_completed.name
              },
              {
                name  = "MAX_CONCURRENCY"
                value = "100"
              },
              {
                name  = "LOG_LEVEL"
                value = "INFO"
              },
              {
                name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
                value = azurerm_application_insights.main.connection_string
              },
              {
                name  = "OTEL_SERVICE_NAME"
                value = "llm-worker"
              },
              {
                name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
                value = "true"
              },
              {
                name  = "AZURE_AI_CHAT_ENDPOINT"
                value = "https://${azapi_resource.ai_service.name}.cognitiveservices.azure.com/openai/deployments/${azurerm_cognitive_deployment.openai_model.name}"
              },
              {
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
                name  = "MEMORY_API_ENDPOINT"
                value = "https://${azapi_resource.memory_api.output.properties.configuration.ingress.fqdn}"
              },
              {
                name  = "MEMORY_API_TIMEOUT"
                value = "2.0"
              }
            ]
          }
        ]
        scale = {
          cooldownPeriod = var.container_app_cooldown_period
          minReplicas    = var.container_app_min_replicas
          maxReplicas    = 10
          rules = [
            {
              name = "service-bus-topic-scale-rule"
              custom = {
                type = "azure-servicebus"
                metadata = {
                  topicName        = azurerm_servicebus_topic.user_messages.name
                  subscriptionName = azurerm_servicebus_subscription.worker_service_user_messages.name
                  namespace        = azurerm_servicebus_namespace.main.name
                  messageCount     = "10"
                },
                identity = azurerm_user_assigned_identity.llm_worker.id
              }
            }
          ]
        }
        # Configure graceful shutdown with 4-minute grace period for LLM processing
        terminationGracePeriodSeconds = 240
      }
    }
  }
}
