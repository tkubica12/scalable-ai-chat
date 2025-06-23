variable "llm_location" {
  type        = string
  default     = "swedencentral"
  description = "Azure region for LLM-related resources."
}
variable "prefix" {
  type        = string
  default     = "chat"
  description = <<EOF
Prefix for resources.
Preferably 2-4 characters long without special characters, lowercase.
EOF
}

variable "location" {
  type        = string
  default     = "germanywestcentral"
  description = <<EOF
Azure region for resources.

Examples: swedencentral, westeurope, northeurope, germanywestcentral.
EOF
}

variable "service_bus_sku" {
  type        = string
  default     = "Standard"
  description = "Service Bus SKU: Standard or Premium"
  validation {
    condition     = contains(["Standard", "Premium"], var.service_bus_sku)
    error_message = "SKU must be either 'Standard' or 'Premium'."
  }
}

variable "github_repository" {
  type        = string
  default     = "tkubica12/scalable-ai-chat"
  description = <<EOF
GitHub repository in the format owner/repo-name.
Used for container image references in the format ghcr.io/owner/repo-name/service-name:tag.

Example: tkubica12/scalable-ai-chat
EOF
}

variable "container_app_cooldown_period" {
  type        = number
  default     = 600
  description = <<EOF
The cooldown period in seconds for container app scaling.
This is the amount of time to wait after a scale operation before allowing another scale operation.

Examples: 300 (5 minutes), 600 (10 minutes), 900 (15 minutes)
EOF
}

variable "container_app_min_replicas" {
  type        = number
  default     = 0
  description = <<EOF
The minimum number of replicas for container apps.
Setting to 0 allows apps to scale to zero when not in use.

Examples: 0 (scale to zero), 1 (always running), 2 (high availability)
EOF
}
