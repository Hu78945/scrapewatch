variable "name" {
  description = "Short prefix for resource names (lowercase letters/digits)."
  type        = string
  default     = "scrapewatch"
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,11}$", var.name))
    error_message = "3-12 lowercase letters/digits (storage account names are global and <= 24 chars)."
  }
}

variable "location" {
  type    = string
  default = "westeurope"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "private_endpoint_subnet_id" {
  description = "Subnet for private endpoints (blob, dfs, vault). Null = no endpoints; the services stay closed to the public internet either way."
  type        = string
  default     = null
}

variable "private_dns_zone_ids" {
  description = "privatelink DNS zone ids keyed by blob / dfs / vault, used when private_endpoint_subnet_id is set."
  type        = map(string)
  default     = {}
}

variable "allowed_ip_ranges" {
  description = "Optional CIDRs allowed through the storage/key vault firewalls (e.g. an operator's egress IP). Empty = none."
  type        = list(string)
  default     = []
}

variable "reader_principal_ids" {
  description = "Azure AD object ids (e.g. an analysts group) granted read-only access to the lake container."
  type        = list(string)
  default     = []
}

variable "retention_days" {
  description = "Soft-delete retention for blobs and containers."
  type        = number
  default     = 14
}
