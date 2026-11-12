output "lake_uri" {
  description = "SCRAPEWATCH_LAKE value for the pipeline."
  value       = "az://${azurerm_storage_container.lake.name}"
}

output "storage_account_blob_endpoint" {
  description = "AZURE_STORAGE_ACCOUNT_URL value (Azure AD auth via the pipeline identity)."
  value       = azurerm_storage_account.lake.primary_blob_endpoint
}

output "dfs_endpoint" {
  value = azurerm_storage_account.lake.primary_dfs_endpoint
}

output "key_vault_uri" {
  value = azurerm_key_vault.this.vault_uri
}

output "pipeline_identity_client_id" {
  description = "Set as AZURE_CLIENT_ID where the pipeline runs so DefaultAzureCredential picks this identity."
  value       = azurerm_user_assigned_identity.pipeline.client_id
}
