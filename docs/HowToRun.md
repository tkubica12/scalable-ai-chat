# How to Run the Scalable AI Chat Project

This document provides instructions on how to deploy and run the Scalable AI Chat project.

## Deployment with Terraform

The project uses Terraform to manage and deploy Azure resources.

### Prerequisites

*   Azure CLI installed and configured.
*   Terraform CLI installed.
*   An Azure subscription.
*   Fork this repository to your own GitHub account to publish container images.

### Steps

1.  **Clone the repository (if you haven't already):**

    ```bash
    git clone <your-fork-url>
    cd scalable-ai-chat
    ```

2.  **Configure Terraform Backend:**
  
    Open `terraform/providers.tf` and configure the backend block. For local testing, you can use the "local" backend. For team collaboration, configure it to use an "azurerm" backend with an Azure Storage Account.


3.  **Initialize Terraform:**
   
    Navigate to the `terraform` directory and run:

    ```bash
    cd terraform
    terraform init
    ```

4.  **Review and Apply Terraform Configuration:**
   
    Review the `terraform/variables.tf` file and update any default values if necessary. You can also create a `terraform.tfvars` file to override default variable values.

    ```bash
    terraform plan
    terraform apply
    ```

    This command will provision the necessary Azure resources.

### Azure Resources Deployed

The Terraform configuration will deploy the following Azure resources:

*   **Azure Resource Group:** A container that holds related resources for an Azure solution.
*   **Azure Container Apps Environment:** A serverless platform for running containerized applications. This environment is configured for:
    *   **Scale to Zero:** Container apps can scale down to zero replicas when not in use, reducing costs. The `container_app_min_replicas` variable in `demo.auto.tfvars` (defaulting to 0) controls this.
    *   **Cooldown Period:** This is the period of inactivity after which a container app scales down. This is managed by Azure Container Apps automatically based on scaling rules.
*   **Azure Container Apps:** Individual microservices are deployed as container apps. These include:
    *   `front_service`
    *   `history_api`
    *   `history_worker`
    *   `llm_worker`
    *   `memory_api`
    *   `memory_worker`
    *   `sse_service`
    *   `web_client`
*   **Azure Cosmos DB:** Used for storing chat history and user memory. Separate databases and containers are created for `history` and `memory`.
*   **Azure Service Bus:** A messaging service used for communication between microservices.
    *   **Performance SKU:** The `service_bus_sku` variable in `demo.auto.tfvars` (defaulting to "Standard") determines the performance tier. Options include Basic, Standard, and Premium, each offering different levels of throughput and features.
*   **Azure OpenAI Service:** Provides access to large language models.
*   **Azure AI Hub/Project:** (via `azapi_resource`) For managing AI services and projects within Azure.
*   **Azure Cache for Redis Enterprise:** Used for caching and session management.
*   **Azure Key Vault:** For securely storing secrets and keys.
*   **Azure Log Analytics Workspace & Application Insights:** For monitoring, logging, and diagnostics.
*   **Azure Storage Account:** Used for various purposes, including potentially Terraform state storage (if configured).
*   **User Assigned Managed Identity:** Provides an Azure Active Directory identity for applications to use when connecting to resources that support AAD authentication.

**Region:** The `location` variable in `terraform/variables.tf` defines the primary Azure region for resource deployment. The `llm_location` variable can be used to specify a different region for LLM deployments if needed (e.g., due to model availability).

## Microservice Builds and Container Images

Each microservice in the `src/` directory has a corresponding GitHub Actions workflow (e.g., `front-service-BUILD.yml`). These workflows are triggered on pushes to their respective service's path.

*   The workflows build Docker container images for each microservice.
*   These images are then published to GitHub Packages.
*   The provided workflows publish to the original repository's packages. **It is crucial to fork this repository and update the workflows to publish images to your own GitHub Packages registry.** The Terraform configuration for Container Apps will then pull these images from your registry.

## Local Deployment and Testing

It's possible to run and test the services locally.

### Configuration

1.  **Create a `.env` file:**
    There is no `.env.sample` file currently in the repository. You will need to create one. This file should define environment variables necessary for the services to run. Based on the project structure, this would typically include:
    *   Service Bus connection strings or relevant AAD details for local authentication.
    *   Cosmos DB connection strings or AAD details.
    *   Redis connection details.
    *   Azure OpenAI endpoint and API key.
    *   URLs for other local or deployed services they need to communicate with.

    Example structure for a `.env` file (this is a hypothetical example, actual variables will depend on each service's needs):

    ```env
    # Service Bus
    SERVICE_BUS_NAMESPACE_HOSTNAME="<your-sb-namespace>.servicebus.windows.net"
    # Cosmos DB
    COSMOS_DB_ENDPOINT="https://<your-cosmos-account>.documents.azure.com:443/"
    # Redis
    REDIS_HOSTNAME="<your-redis-host>"
    REDIS_PORT="<your-redis-port>"
    REDIS_PASSWORD="<your-redis-password>" # If using basic auth, otherwise AAD details
    # Azure OpenAI
    AZURE_OPENAI_ENDPOINT="<your-aoai-endpoint>"
    AZURE_OPENAI_API_KEY="<your-aoai-key>" # Or configure AAD auth

    # Example for a specific service if it needs to know another service's URL
    HISTORY_API_URL="http://localhost:8001"
    ```

2.  **Azure Authentication:**
    When running services locally, they can leverage your local Azure login credentials (e.g., from Azure CLI `az login` or Azure PowerShell `Connect-AzAccount`). Ensure your local user account has the necessary RBAC permissions on the Azure services (Service Bus, Cosmos DB, Redis, Azure OpenAI) that were deployed by Terraform. The Terraform scripts in `terraform/rbac.*.tf` set up roles for the managed identities of the deployed services; you may need to grant similar roles to your user account for local development if you are not using service principal credentials directly in your `.env` file.

### Running Services

Each service is a Python application (except `web_client` which is Node.js/Svelte). To run them locally:

1.  Navigate to the service directory (e.g., `src/front_service`).
2.  Run the main application file (e.g., `main.py`).
  
    ```bash
    uv run main.py
    ```

    For the `web_client`, you would typically use npm or yarn:

    ```bash
    cd src/web_client
    npm install
    npm run dev
    ```

Refer to the `README.md` file within each service's directory for more specific instructions on building and running that particular service.
