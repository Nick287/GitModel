locals {
  # SQLite is WAL-mode and single-writer: never run more than one replica.
  # (The Azure Files SMB mount uses nobrl so locking works — see the volume
  # block on azurerm_container_app.hub.)
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

  # SecurityControl=Ignore MUST be present the instant the RG is created, so the
  # org security policy doesn't block resources (e.g. the storage account) made
  # inside it. In Terraform, tags are part of the RG's create API call — they're
  # applied atomically with the RG, before anything that depends on it. Every
  # resource below references azurerm_resource_group.rg, so they are all created
  # strictly after this tag exists.
  tags = {
    SecurityControl = "Ignore"
  }
}

# --- Container registry (create if missing, else use existing) ------------
#
# Terraform can't probe for existence at plan time, so `create_acr` selects the
# mode: true (default) -> create a new registry in the app's resource group
# with a globally-unique name (prefix + random suffix); false -> look up an
# existing registry named acr_name in acr_resource_group_name. Either way the
# rest of the config reads through local.acr_* so nothing else changes.

resource "random_string" "acr_suffix" {
  count   = var.create_acr ? 1 : 0
  length  = 8
  upper   = false
  special = false
}

resource "azurerm_container_registry" "acr" {
  count               = var.create_acr ? 1 : 0
  name                = "${var.prefix}${random_string.acr_suffix[0].result}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
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
# A dedicated storage account + file share in the app's resource group.
# Key-based mounts are the default; managed identity mode disables shared keys.
# The account name uses a random suffix for global uniqueness.

resource "random_string" "storage_suffix" {
  length  = 8
  upper   = false
  special = false
}

resource "azurerm_storage_account" "data" {
  name                      = "${var.prefix}${random_string.storage_suffix.result}"
  resource_group_name       = azurerm_resource_group.rg.name
  location                  = azurerm_resource_group.rg.location
  account_tier              = "Standard"
  account_replication_type  = "LRS"
  local_user_enabled        = !var.storage_managed_identity
  shared_access_key_enabled = !var.storage_managed_identity
  # The SMB managed identity mount exchanges its token for a Kerberos ticket
  # through the account's public endpoint, not the private endpoint, so that
  # mode needs public access even with private_storage. With shared keys off,
  # only RBAC-authorized Entra identities can use it. Once /data is on NFS
  # this account no longer serves the app and closes again.
  public_network_access_enabled = !var.private_storage || (var.storage_managed_identity && !local.nfs)
  # The /data share holds the SQLite DB; nothing here should ever be anonymously
  # reachable. Pin blob public access off (also the org-policy default), which
  # keeps `terraform plan` clean instead of drifting back to the schema default.
  allow_nested_items_to_be_public = false
}

resource "azurerm_storage_share" "data" {
  name               = var.file_share_name
  storage_account_id = azurerm_storage_account.data.id
  quota              = 5
}

# --- Container Apps environment + app -------------------------------------

resource "azurerm_container_app_environment" "env" {
  name                     = coalesce(var.environment_name, "${var.prefix}-env")
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  infrastructure_subnet_id = var.private_storage ? azurerm_subnet.apps[0].id : null

  dynamic "identity" {
    for_each = var.storage_managed_identity ? [1] : []
    content {
      type         = "UserAssigned"
      identity_ids = [azurerm_user_assigned_identity.app.id]
    }
  }

  dynamic "workload_profile" {
    for_each = var.private_storage ? [1] : []
    content {
      name                  = "Consumption"
      workload_profile_type = "Consumption"
    }
  }
}

resource "azurerm_container_app_environment_storage" "data" {
  name                         = "${var.prefix}-data"
  container_app_environment_id = azurerm_container_app_environment.env.id
  account_name                 = azurerm_storage_account.data.name
  access_key                   = azurerm_storage_account.data.primary_access_key
  share_name                   = azurerm_storage_share.data.name
  access_mode                  = "ReadWrite"

  depends_on = [azurerm_private_endpoint.files, azurerm_private_dns_zone_virtual_network_link.files]

  # An env storage can't be deleted while the app's volume still references it.
  # create_before_destroy flips the replace order to: create the new storage →
  # repoint the app volume → delete the old storage, avoiding the
  # ManagedEnvironmentStorageDeleteInUse error on a destroy-first replace.
  lifecycle {
    create_before_destroy = true
  }
}

resource "azurerm_container_app" "hub" {
  name                         = "${var.prefix}-hub"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"
  workload_profile_name        = var.private_storage ? "Consumption" : null

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

    dynamic "init_container" {
      for_each = var.storage_managed_identity || local.nfs ? [1] : []
      content {
        name    = "verify-storage"
        image   = "${local.acr_login_server}/${local.image_ref}"
        cpu     = 0.25
        memory  = "0.5Gi"
        command = ["python", "-c"]
        args = [<<-PYTHON
          import os
          import sqlite3
          from contextlib import closing
          from pathlib import Path
          if not os.path.ismount('/data'):
              raise RuntimeError('Azure Files is not mounted at /data')
          database = Path('/data/hub.db')
          if database.exists():
              if database.stat().st_size == 0:
                  raise RuntimeError('Existing database is empty; refusing to initialize it')
              with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=30)) as connection:
                  if connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kv'").fetchone() is None:
                      raise RuntimeError('Existing database has no kv table')
                  if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                      raise RuntimeError('Existing database failed integrity check')
          print('Azure Files mount verified', flush=True)
        PYTHON
        ]

        volume_mounts {
          name = "data"
          path = "/data"
        }
      }
    }

    volume {
      name         = "data"
      storage_type = local.nfs ? "NfsAzureFile" : "AzureFile"
      storage_name = (
        local.nfs ? azurerm_container_app_environment_storage.nfs[0].name :
        var.storage_managed_identity ? azapi_resource.identity_storage[0].name :
        azurerm_container_app_environment_storage.data.name
      )
      # SQLite needs file locking that Azure Files (SMB) byte-range locks don't
      # provide, causing "database is locked". nobrl disables byte-range locks
      # (safe with a single replica = single writer). Paired with a non-WAL
      # journal mode (HUB_DB_JOURNAL_MODE) since WAL's -shm mmap is unsupported
      # on SMB too. NFS locks work as-is, and Container Apps ignores NFS mount
      # options anyway.
      mount_options = local.nfs ? null : "nobrl"
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

      # /data is an Azure Files (SMB or NFS) mount; SQLite WAL relies on mmap
      # shared memory that network filesystems don't support. Use a
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
