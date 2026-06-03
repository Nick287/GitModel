locals {
  # SQLite is WAL-mode and single-writer: never run more than one replica.
  replicas = 1
}

resource "azurerm_resource_group" "rg" {
  name     = var.resource_group_name
  location = var.location
}

# Existing ACR that already holds the gitmodel image.
data "azurerm_container_registry" "acr" {
  name                = var.acr_name
  resource_group_name = var.acr_resource_group_name
}

# --- Identity used by the Container App to pull from ACR ------------------

resource "azurerm_user_assigned_identity" "app" {
  name                = "${var.prefix}-id"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = data.azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- Persistent storage for /data -----------------------------------------
#
# Uses an existing storage account (in another RG) that allows shared keys,
# since Container Apps' Azure Files mount requires key-based auth.

data "azurerm_storage_account" "data" {
  name                = var.storage_account_name
  resource_group_name = var.storage_account_resource_group
}

# --- Container Apps environment + app -------------------------------------

resource "azurerm_container_app_environment" "env" {
  name                = "${var.prefix}-env"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
}

resource "azurerm_container_app_environment_storage" "data" {
  name                         = "${var.prefix}-data"
  container_app_environment_id = azurerm_container_app_environment.env.id
  account_name                 = data.azurerm_storage_account.data.name
  access_key                   = data.azurerm_storage_account.data.primary_access_key
  share_name                   = var.file_share_name
  access_mode                  = "ReadWrite"
}

resource "azurerm_container_app" "hub" {
  name                         = "${var.prefix}-hub"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = data.azurerm_container_registry.acr.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  ingress {
    external_enabled = true
    target_port      = var.container_port
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = local.replicas
    max_replicas = local.replicas

    volume {
      name         = "data"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.data.name
    }

    container {
      name   = "${var.prefix}-hub"
      image  = "${data.azurerm_container_registry.acr.login_server}/${var.image_tag}"
      cpu    = var.cpu
      memory = var.memory

      env {
        name  = "HUB_DATA_DIR"
        value = "/data"
      }

      env {
        name  = "HUB_REQUIRE_AUTH"
        value = tostring(var.require_auth)
      }

      env {
        name  = "HUB_LOGIN_MAX_FAILS"
        value = tostring(var.login_max_fails)
      }

      env {
        name  = "HUB_LOGIN_LOCK_SECONDS"
        value = tostring(var.login_lock_seconds)
      }

      volume_mounts {
        name = "data"
        path = "/data"
      }
    }
  }

  depends_on = [azurerm_role_assignment.acr_pull]
}
