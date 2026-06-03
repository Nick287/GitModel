output "app_url" {
  description = "Public HTTPS URL of the GitModel Hub."
  value       = "https://${azurerm_container_app.hub.ingress[0].fqdn}"
}

output "resource_group" {
  value = azurerm_resource_group.rg.name
}
