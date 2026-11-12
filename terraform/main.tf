# Azure footprint for scrapewatch: an ADLS Gen2 lake, a Key Vault for secrets, and one managed identity for the
# pipeline with exactly the roles it needs. Private by default: no public network access, no shared keys,
# Azure AD only, TLS 1.2+. Validated in CI with `terraform validate`; never applied by this repo.

data "azurerm_client_config" "current" {}

locals {
  suffix = substr(md5("${var.name}-${var.environment}-${var.location}"), 0, 6)
  tags   = { project = "scrapewatch", environment = var.environment, managed_by = "terraform" }
  private_endpoints = var.private_endpoint_subnet_id == null ? {} : {
    blob  = { id = azurerm_storage_account.lake.id, subresource = "blob" }
    dfs   = { id = azurerm_storage_account.lake.id, subresource = "dfs" }
    vault = { id = azurerm_key_vault.this.id, subresource = "vault" }
  }
}

resource "azurerm_resource_group" "this" {
  name     = "rg-${var.name}-${var.environment}"
  location = var.location
  tags     = local.tags
}

# ---- data lake (ADLS Gen2) ---------------------------------------------------------------------------------------
resource "azurerm_storage_account" "lake" {
  name                     = "st${var.name}${var.environment}${local.suffix}"
  resource_group_name      = azurerm_resource_group.this.name
  location                 = azurerm_resource_group.this.location
  account_kind             = "StorageV2"
  account_tier             = "Standard"
  account_replication_type = "ZRS"
  is_hns_enabled           = true # hierarchical namespace = ADLS Gen2

  min_tls_version                   = "TLS1_2"
  https_traffic_only_enabled        = true
  shared_access_key_enabled         = false # Azure AD only: no account keys, no SAS signed with them
  default_to_oauth_authentication   = true
  allow_nested_items_to_be_public   = false
  public_network_access_enabled     = false
  infrastructure_encryption_enabled = true # double encryption at rest

  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
    ip_rules       = var.allowed_ip_ranges
  }

  blob_properties {
    delete_retention_policy {
      days = var.retention_days
    }
    container_delete_retention_policy {
      days = var.retention_days
    }
  }

  tags = local.tags
}

# Created through the ARM control plane (storage_account_id), so it works with public network access disabled.
# bronze/, silver/ and ops/ are prefixes (directories) inside this filesystem.
resource "azurerm_storage_container" "lake" {
  name                  = "lake"
  storage_account_id    = azurerm_storage_account.lake.id
  container_access_type = "private"
}

# Raw HTML expires from the hot tier: bronze pages move to cool after 30 days and are deleted after 180.
resource "azurerm_storage_management_policy" "lake" {
  storage_account_id = azurerm_storage_account.lake.id
  rule {
    name    = "bronze-pages-lifecycle"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["lake/bronze/pages/"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than = 30
        delete_after_days_since_modification_greater_than       = 180
      }
    }
  }
}

# ---- secrets -----------------------------------------------------------------------------------------------------
resource "azurerm_key_vault" "this" {
  name                          = "kv-${var.name}-${var.environment}-${local.suffix}"
  resource_group_name           = azurerm_resource_group.this.name
  location                      = azurerm_resource_group.this.location
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = false

  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
    ip_rules       = var.allowed_ip_ranges
  }

  tags = local.tags
}

# ---- identity + least-privilege RBAC ----------------------------------------------------------------------------
resource "azurerm_user_assigned_identity" "pipeline" {
  name                = "id-${var.name}-pipeline-${var.environment}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = local.tags
}

# Read/write blobs in the one container it uses - not account-wide, no management-plane rights.
resource "azurerm_role_assignment" "pipeline_lake" {
  scope                = azurerm_storage_container.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.pipeline.principal_id
}

# Read secret values (e.g. the alert webhook URL). Cannot list, write or delete them.
resource "azurerm_role_assignment" "pipeline_secrets" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.pipeline.principal_id
}

resource "azurerm_role_assignment" "readers" {
  for_each             = toset(var.reader_principal_ids)
  scope                = azurerm_storage_container.lake.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = each.value
}

# ---- optional private endpoints ---------------------------------------------------------------------------------
resource "azurerm_private_endpoint" "this" {
  for_each            = local.private_endpoints
  name                = "pe-${var.name}-${each.key}-${var.environment}"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "psc-${each.key}"
    private_connection_resource_id = each.value.id
    subresource_names              = [each.value.subresource]
    is_manual_connection           = false
  }

  dynamic "private_dns_zone_group" {
    for_each = contains(keys(var.private_dns_zone_ids), each.key) ? [1] : []
    content {
      name                 = "default"
      private_dns_zone_ids = [var.private_dns_zone_ids[each.key]]
    }
  }

  tags = local.tags
}
