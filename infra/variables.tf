variable "resource_group_name" {
  type        = string
  description = "Resource group to create for the Container App resources."
  default     = "gitmodel-rg"
}

variable "location" {
  type        = string
  description = "Azure region for all resources."
  default     = "centralus"
}

variable "prefix" {
  type        = string
  description = "Short name prefix used for resource names (lowercase, no spaces)."
  default     = "gitmodel"
}

# --- Container image (built in-cloud by `az acr build`) -------------------

variable "image_name" {
  type        = string
  description = "Image repository name inside the registry. The tag is derived automatically from a hash of the source (hub/, Dockerfile, requirements.txt)."
  default     = "gitmodel"
}

# --- Persistent storage (existing account that allows shared keys) ---------

variable "storage_connection_string" {
  type        = string
  sensitive   = true
  description = "Connection string of an existing storage account (must allow shared key access) used for the /data Azure Files mount. account_name and access_key are parsed from it, so Terraform needs no RBAC on the account — works across subscriptions/tenants."
}

variable "file_share_name" {
  type        = string
  description = "Azure Files share mounted at /data."
  default     = "gitmodel-data"
}

# --- App runtime settings -------------------------------------------------

variable "container_port" {
  type        = number
  description = "Port the app listens on inside the container."
  default     = 8088
}

variable "require_auth" {
  type        = bool
  description = "Set HUB_REQUIRE_AUTH. Keep true when exposed to the public internet."
  default     = true
}

variable "login_max_fails" {
  type        = number
  description = "HUB_LOGIN_MAX_FAILS: consecutive failed admin logins from one IP before lockout. Set 0 to disable throttling."
  default     = 5
}

variable "login_lock_seconds" {
  type        = number
  description = "HUB_LOGIN_LOCK_SECONDS: how long (seconds) an IP stays locked after too many failed admin logins."
  default     = 900
}

variable "cpu" {
  type        = number
  description = "vCPU per replica."
  default     = 0.5
}

variable "memory" {
  type        = string
  description = "Memory per replica (must pair with cpu, e.g. 1.0Gi for 0.5 vCPU)."
  default     = "1.0Gi"
}
