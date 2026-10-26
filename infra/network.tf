resource "azurerm_virtual_network" "app" {
  count               = var.private_storage ? 1 : 0
  name                = "${var.prefix}-vnet"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  address_space       = ["10.42.0.0/16"]
}

resource "azurerm_subnet" "apps" {
  count                = var.private_storage ? 1 : 0
  name                 = "container-apps"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.app[0].name
  address_prefixes     = ["10.42.0.0/23"]

  delegation {
    name = "container-apps"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "endpoints" {
  count                = var.private_storage ? 1 : 0
  name                 = "private-endpoints"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.app[0].name
  address_prefixes     = ["10.42.2.0/24"]
}

resource "azurerm_private_dns_zone" "files" {
  count               = var.private_storage ? 1 : 0
  name                = "privatelink.file.core.windows.net"
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "files" {
  count                 = var.private_storage ? 1 : 0
  name                  = "${var.prefix}-files"
  resource_group_name   = azurerm_resource_group.rg.name
  private_dns_zone_name = azurerm_private_dns_zone.files[0].name
  virtual_network_id    = azurerm_virtual_network.app[0].id
  registration_enabled  = false
}

resource "azurerm_private_endpoint" "files" {
  count               = var.private_storage ? 1 : 0
  name                = "${var.prefix}-files-pe"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  subnet_id           = azurerm_subnet.endpoints[0].id

  private_service_connection {
    name                           = "${var.prefix}-files"
    private_connection_resource_id = azurerm_storage_account.data.id
    subresource_names              = ["file"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "files"
    private_dns_zone_ids = [azurerm_private_dns_zone.files[0].id]
  }
}