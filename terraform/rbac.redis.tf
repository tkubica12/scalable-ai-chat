resource "azapi_resource" "redis_access_self" {
  type      = "Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments@2024-09-01-preview"
  name      = "self"
  parent_id = azapi_resource.redis_db.id
  body = {
    properties = {
      accessPolicyName = "default"
      user = {
        objectId = data.azurerm_client_config.current.object_id
      }
    }
  }
}

resource "azapi_resource" "redis_access_history_worker" {
  type      = "Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments@2024-09-01-preview"
  name      = "historyworker"
  parent_id = azapi_resource.redis_db.id
  body = {
    properties = {
      accessPolicyName = "default"
      user = {
        objectId = azurerm_user_assigned_identity.history_worker.principal_id
      }
    }
  }
}

resource "azapi_resource" "redis_access_memory_worker" {
  type      = "Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments@2024-09-01-preview"
  name      = "memoryworker"
  parent_id = azapi_resource.redis_db.id
  body = {
    properties = {
      accessPolicyName = "default"
      user = {
        objectId = azurerm_user_assigned_identity.memory_worker.principal_id
      }
    }
  }
}

resource "azapi_resource" "redis_access_llm_worker" {
  type      = "Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments@2024-09-01-preview"
  name      = "llmworker"
  parent_id = azapi_resource.redis_db.id
  body = {
    properties = {
      accessPolicyName = "default"
      user = {
        objectId = azurerm_user_assigned_identity.llm_worker.principal_id
      }
    }
  }
}

resource "azapi_resource" "redis_access_front_service" {
  type      = "Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments@2024-09-01-preview"
  name      = "frontservice"
  parent_id = azapi_resource.redis_db.id
  body = {
    properties = {
      accessPolicyName = "default"
      user = {
        objectId = azurerm_user_assigned_identity.front_service.principal_id
      }
    }
  }
}

resource "azapi_resource" "redis_access_sse_service" {
  type      = "Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments@2024-09-01-preview"
  name      = "sseservice"
  parent_id = azapi_resource.redis_db.id
  body = {
    properties = {
      accessPolicyName = "default"
      user = {
        objectId = azurerm_user_assigned_identity.sse_service.principal_id
      }
    }
  }
}