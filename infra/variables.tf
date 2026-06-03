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

# --- Container image (already pushed to ACR) ------------------------------

variable "acr_name" {
  type        = string
  description = "Name of the existing Azure Container Registry (without .azurecr.io)."
  default     = "<your-acr-name>"
}

variable "acr_resource_group_name" {
  type        = string
  description = "Resource group that contains the existing ACR."
  default     = "<your-acr-resource-group>"
}

variable "image_tag" {
  type        = string
  description = "Image repository:tag inside the ACR."
  default     = "gitmodel:latest"
}

# --- Persistent storage (existing account that allows shared keys) ---------

variable "storage_account_name" {
  type        = string
  description = "Existing storage account used for the /data Azure Files mount (must allow shared key access)."
  default     = "<your-storage-account>"
}

variable "storage_account_resource_group" {
  type        = string
  description = "Resource group that contains the storage account."
  default     = "<your-storage-resource-group>"
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
