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

# --- Container registry (create if missing, else use existing) ------------

variable "create_acr" {
  type        = bool
  description = "true: create a new Azure Container Registry named acr_name. false: use an existing registry with that name. Terraform can't auto-detect existence, so set this to match reality."
  default     = false
}

variable "acr_name" {
  type        = string
  description = "Name of the Azure Container Registry to build into and pull from (created when create_acr = true, otherwise looked up)."
  default     = "azureaidemoacr"
}

variable "acr_resource_group_name" {
  type        = string
  description = "Resource group of the Azure Container Registry."
  default     = "rg-azureaidemo"
}

# --- Container image (built in-cloud by `az acr build`) -------------------

variable "image_name" {
  type        = string
  description = "Image repository name inside the registry. The tag is derived automatically from a hash of the source (hub/, Dockerfile, requirements.txt)."
  default     = "gitmodel"
}

# --- Persistent storage (created by Terraform in the app's resource group) -

variable "file_share_name" {
  type        = string
  description = "Azure Files share (created by Terraform) mounted at /data."
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
