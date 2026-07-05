# Microsoft Foundry resource (Microsoft.CognitiveServices/accounts, kind = AIServices).
# `allowProjectManagement = true` is what turns this Cognitive Services account into a
# Foundry resource capable of hosting Foundry projects as child resources (see ADR-008).
resource "azapi_resource" "ai_service" {
  type      = "Microsoft.CognitiveServices/accounts@2025-04-01-preview"
  name      = "aidemo-${local.base_name}"
  location  = var.llm_location
  parent_id = azurerm_resource_group.main.id

  identity {
    type = "SystemAssigned"
  }

  body = {
    name = "aidemo-${local.base_name}"
    properties = {
      # restore             = true
      customSubDomainName    = "aidemo-${local.base_name}"
      allowProjectManagement = true
      #   apiProperties = {
      #     statisticsEnabled = false
      #   }
    }
    kind = "AIServices"
    sku = {
      name = "S0"
    }
  }
  response_export_values = ["*"]
}

# Foundry project, a child resource of the Foundry resource above. Replaces the legacy
# Microsoft.MachineLearningServices hub/project workspaces and their AIServices connection
# (see ADR-008 in REFACTOR_PLAN.md). The azurerm provider version pinned in this project
# (~> 4, currently 4.28.x) does not yet expose a native `azurerm_cognitive_account_project`
# resource (that lands in azurerm >= 4.55), so azapi is used against the current
# Microsoft.CognitiveServices/accounts/projects control-plane API instead, per ADR-008.
resource "azapi_resource" "ai_project" {
  type      = "Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview"
  name      = "aidemo-project-${local.base_name}"
  location  = var.llm_location
  parent_id = azapi_resource.ai_service.id

  identity {
    type = "SystemAssigned"
  }

  body = {
    properties = {
      displayName = "AI demo Project"
      description = "Foundry project for the scalable AI chat demo."
    }
  }
}
