// Reads a Function App's current app settings so the caller can merge its declared values
// over them. siteConfig.appSettings replaces the whole set, so without this a redeploy erases
// every secret that was set out of band. It is a module because reading a site's settings in
// the template that also declares the site is rejected as a circular dependency.

@description('Function App whose current app settings are read')
param functionAppName string

@description('False when the app is being created and there is nothing to read yet')
param appExists bool

// Secure, so the secret values are never written to deployment history.
@secure()
output settings object = appExists ? list(resourceId('Microsoft.Web/sites/config', functionAppName, 'appsettings'), '2024-04-01').properties : {}
