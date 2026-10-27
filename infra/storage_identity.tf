resource "azapi_update_resource" "storage_smb_oauth" {
  count       = var.storage_managed_identity ? 1 : 0
  type        = "Microsoft.Storage/storageAccounts@2025-08-01"
  resource_id = azurerm_storage_account.data.id

  body = {
    properties = {
      azureFilesIdentityBasedAuthentication = {
        directoryServiceOptions = "None"
        smbOAuthSettings = {
          isSmbOAuthEnabled = true
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = length(azurerm_storage_account.data.azure_files_authentication) == 0
      error_message = "Managed identity mode must not replace an existing directory authentication configuration."
    }
  }
}

resource "azapi_update_resource" "storage_smb_kerberos" {
  count       = var.storage_managed_identity ? 1 : 0
  type        = "Microsoft.Storage/storageAccounts/fileServices@2025-08-01"
  resource_id = "${azurerm_storage_account.data.id}/fileServices/default"

  body = {
    properties = {
      protocolSettings = {
        smb = {
          authenticationMethods = "NTLMv2;Kerberos"
        }
      }
    }
  }
}

resource "azurerm_role_assignment" "storage_smb" {
  count                = var.storage_managed_identity ? 1 : 0
  scope                = azurerm_storage_account.data.id
  role_definition_name = "Storage File Data SMB MI Admin"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azapi_resource" "identity_storage" {
  count                     = var.storage_managed_identity ? 1 : 0
  type                      = "Microsoft.App/managedEnvironments/storages@2026-03-02-preview"
  parent_id                 = azurerm_container_app_environment.env.id
  name                      = "${var.prefix}-data-mi"
  schema_validation_enabled = false

  body = {
    properties = {
      azureFile = {
        accountName = azurerm_storage_account.data.name
        shareName   = azurerm_storage_share.data.name
        accessMode  = "ReadWrite"
        identity    = azurerm_user_assigned_identity.app.id
      }
    }
  }

  depends_on = [
    azapi_update_resource.storage_smb_oauth,
    azapi_update_resource.storage_smb_kerberos,
    azurerm_role_assignment.storage_smb,
    azurerm_private_endpoint.files,
    azurerm_private_dns_zone_virtual_network_link.files
  ]
}