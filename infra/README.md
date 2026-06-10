# Infra

Lean V1 Azure footprint (Bicep or Terraform): App Service or Container Apps (MCP server), PostgreSQL
Flexible Server (registry), Azure AI Search (one derived index), Azure OpenAI endpoint, GitHub Actions
nightly build, Application Insights/Azure Monitor, Managed Identity + Key Vault.

Excluded in V1 (ADR-0007): Functions, Event Grid, Service Bus/Event Hub, Redis, API Management, Blob,
graph DB. Prefer managed identity over Key Vault secrets wherever supported.
