# --- Optional NFS storage for /data ----------------------------------------
#
# NFS shares need no storage key, token or Kerberos ticket: access is granted
# by network alone, so the account keeps public network access disabled and
# is reached only through its private endpoint. This avoids both failure modes
# seen with SMB in locked-down subscriptions: shared keys being disabled by
# policy, and the SMB managed identity mount needing the public endpoint for
# its Kerberos ticket exchange.
#
# NFS requires Premium (SSD) storage; provisioned v2 at the 32 GiB minimum
# includes the baseline 3,000 IOPS and 100 MiB/s at no extra cost. Locks are
# enforced server-side (Container Apps ignores NFS mount options, so
# local_lock can't be used), which makes each SQLite transaction a network
# round trip but keeps locking real rather than disabled as with SMB nobrl.

locals {
  nfs = var.storage_protocol == "NFS"
}

resource "azurerm_storage_account" "nfs" {
  count                             = local.nfs ? 1 : 0
  name                              = "${var.prefix}nfs${random_string.storage_suffix.result}"
  resource_group_name               = azurerm_resource_group.rg.name
  location                          = azurerm_resource_group.rg.location
  account_kind                      = "FileStorage"
  account_tier                      = "Premium"
  account_replication_type          = "LRS"
  provisioned_billing_model_version = "V2"
  local_user_enabled                = false
  shared_access_key_enabled         = false
  public_network_access_enabled     = false
  allow_nested_items_to_be_public   = false

  lifecycle {
    precondition {
      condition     = var.private_storage
      error_message = "storage_protocol = \"NFS\" requires private_storage = true: NFS is reachable only through the private endpoint."
    }
  }
}

# Container Apps doesn't support encryption in transit for NFS. Turning it off
# on the file service (rather than disabling secure transfer for the whole
# account) keeps HTTPS-only enforced everywhere else.
resource "azapi_update_resource" "nfs_transit" {
  count       = local.nfs ? 1 : 0
  type        = "Microsoft.Storage/storageAccounts/fileServices@2025-08-01"
  resource_id = "${azurerm_storage_account.nfs[0].id}/fileServices/default"

  body = {
    properties = {
      protocolSettings = {
        nfs = {
          encryptionInTransit = {
            required = false
          }
        }
      }
    }
  }
}

# azurerm_storage_share can't set root squash. The app runs as root, which the
# default RootSquash would map to nobody and deny writes.
resource "azapi_resource" "nfs_share" {
  count     = local.nfs ? 1 : 0
  type      = "Microsoft.Storage/storageAccounts/fileServices/shares@2025-08-01"
  parent_id = "${azurerm_storage_account.nfs[0].id}/fileServices/default"
  name      = var.file_share_name

  body = {
    properties = {
      enabledProtocols = "NFS"
      rootSquash       = "NoRootSquash"
      shareQuota       = 32
    }
  }
}

resource "azurerm_private_endpoint" "nfs" {
  count               = local.nfs ? 1 : 0
  name                = "${var.prefix}-nfs-pe"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  subnet_id           = azurerm_subnet.endpoints[0].id

  private_service_connection {
    name                           = "${var.prefix}-nfs"
    private_connection_resource_id = azurerm_storage_account.nfs[0].id
    subresource_names              = ["file"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "files"
    private_dns_zone_ids = [azurerm_private_dns_zone.files[0].id]
  }
}

resource "azurerm_container_app_environment_storage" "nfs" {
  count                        = local.nfs ? 1 : 0
  name                         = "${var.prefix}-data-nfs"
  container_app_environment_id = azurerm_container_app_environment.env.id
  nfs_server_url               = "${azurerm_storage_account.nfs[0].name}.file.core.windows.net"
  share_name                   = "/${azurerm_storage_account.nfs[0].name}/${azapi_resource.nfs_share[0].name}"
  access_mode                  = "ReadWrite"

  depends_on = [
    azapi_update_resource.nfs_transit,
    azurerm_private_endpoint.nfs,
    azurerm_private_dns_zone_virtual_network_link.files
  ]
}
