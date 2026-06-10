locals {
  # SQLite is WAL-mode and single-writer: never run more than one replica.
  replicas = 1

  # Build context is the repo root (one level up from this module).
  build_context = abspath("${path.module}/..")

  # Stable hash of everything baked into the image: the hub/ package plus the
  # Dockerfile and requirements.txt. __pycache__/*.pyc are excluded so stray
  # bytecode never perturbs the hash (keeps apply idempotent).
  src_files = sort([
    for f in setunion(fileset(local.build_context, "hub/**"),
    toset(["Dockerfile", "requirements.txt"])) :
    f if !can(regex("__pycache__|\\.py[cod]$", f))
  ])
  src_hash = sha1(join("\n",
  [for f in local.src_files : "${f}=${filesha1("${local.build_context}/${f}")}"]))

  # Image ref whose tag changes only when the source changes — so a code edit
  # produces a new image string and the Container App rolls a fresh revision.
  image_ref = "${var.image_name}:${substr(local.src_hash, 0, 12)}"
}

resource "azurerm_resource_group" "rg" {
  name     = var.resource_group_name
  location = var.location
}

# --- Container registry (create if missing, else use existing) ------------
#
# Terraform can't probe for existence at plan time, so `create_acr` selects the
# mode: true -> create a new registry; false -> look up an existing one. Either
# way the rest of the config reads through local.acr_* so nothing else changes.

resource "azurerm_container_registry" "acr" {
  count               = var.create_acr ? 1 : 0
  name                = var.acr_name
  resource_group_name = var.acr_resource_group_name
  location            = var.location
  sku                 = "Basic" # supports ACR Tasks (cloud build) + MI pull
  admin_enabled       = false
}

data "azurerm_container_registry" "acr" {
  count               = var.create_acr ? 0 : 1
  name                = var.acr_name
  resource_group_name = var.acr_resource_group_name
}

locals {
  acr_id           = var.create_acr ? azurerm_container_registry.acr[0].id : data.azurerm_container_registry.acr[0].id
  acr_name         = var.create_acr ? azurerm_container_registry.acr[0].name : data.azurerm_container_registry.acr[0].name
  acr_login_server = var.create_acr ? azurerm_container_registry.acr[0].login_server : data.azurerm_container_registry.acr[0].login_server
}

# Cloud build via ACR Tasks — uploads the build context and builds/pushes the
# image without a local docker daemon. Re-runs only when src_hash changes.
#
# The context is staged into a clean temp dir holding only what the image needs
# (Dockerfile, requirements.txt, hub/). Packing the repo root directly is
# fragile: `az acr build` ignores .dockerignore here and would try to tar large
# / locked files (e.g. infra/.terraform, the live tfstate) and fail with EACCES.
resource "terraform_data" "build" {
  triggers_replace = local.src_hash

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -eu
      ctx="${local.build_context}"
      staging="$(mktemp -d)"
      trap 'rm -rf "$staging"' EXIT
      cp "$ctx/Dockerfile" "$ctx/requirements.txt" "$staging/"
      cp -r "$ctx/hub" "$staging/hub"
      find "$staging/hub" -name __pycache__ -type d -prune -exec rm -rf {} +
      az acr build --registry ${local.acr_name} --image ${local.image_ref} "$staging"
    EOT
  }
}

# --- Identity used by the Container App to pull from ACR ------------------

resource "azurerm_user_assigned_identity" "app" {
  name                = "${var.prefix}-id"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = local.acr_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- Persistent storage for /data -----------------------------------------
#
# Uses an existing storage account that allows shared keys (Container Apps'
# Azure Files mount requires key-based auth). The account name + key are parsed
# straight out of the connection string, so Terraform needs no RBAC on the
# account — it can live in any subscription or tenant.

locals {
  storage_account_name = regex("AccountName=([^;]+)", var.storage_connection_string)[0]
  storage_access_key   = regex("AccountKey=([^;]+)", var.storage_connection_string)[0]
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
  account_name                 = local.storage_account_name
  access_key                   = local.storage_access_key
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
    server   = local.acr_login_server
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
      # SQLite needs file locking that Azure Files (SMB) byte-range locks don't
      # provide, causing "database is locked". nobrl disables byte-range locks
      # (safe with a single replica = single writer). Paired with a non-WAL
      # journal mode (HUB_DB_JOURNAL_MODE) since WAL's -shm mmap is unsupported
      # on SMB too.
      mount_options = "nobrl"
    }

    container {
      name   = "${var.prefix}-hub"
      image  = "${local.acr_login_server}/${local.image_ref}"
      cpu    = var.cpu
      memory = var.memory

      env {
        name  = "HUB_DATA_DIR"
        value = "/data"
      }

      # /data is an Azure Files (SMB) mount; SQLite WAL relies on mmap shared
      # memory unavailable there and fails with "database is locked". Use a
      # rollback-journal mode that works on network filesystems (safe here
      # because the app runs as a single replica = single writer).
      env {
        name  = "HUB_DB_JOURNAL_MODE"
        value = "TRUNCATE"
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

  depends_on = [azurerm_role_assignment.acr_pull, terraform_data.build]
}
