# Terraform CLI configuration for terraform-mirror
#
# Put this in ~/.terraformrc (or point TF_CLI_CONFIG_FILE at it), then use module
# sources exactly as before — `terraform init` now goes through the proxy.

# ---------------------------------------------------------------------------
# Option A (recommended): keep using registry.terraform.io in your module
# sources and send the module API to the proxy.
#
#   source = "registry.terraform.io/hashicorp/consul/aws"   # unchanged
#   source = "hashicorp/consul/aws"                        # also unchanged
# ---------------------------------------------------------------------------
host "registry.terraform.io" {
  services = {
    modules.v1 = "http://127.0.0.1:8080/v1/modules/"
  }
}

# ---------------------------------------------------------------------------
# Option B: a private registry hostname used explicitly in module sources.
# The proxy serves /.well-known/terraform.json, so no `services` block is needed.
#
#   source = "tf.example.com/hashicorp/consul/aws"
#
# host "tf.example.com" {
#   host = "tf.example.com"
#   # plain HTTP only for local experiments; use TLS in production
# }
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Option C: pin the proxy to a specific TLS certificate (self-signed CA example)
#
# host "tf.example.com" {
#   host = "tf.example.com"
#   services = {
#     modules.v1 = "https://tf.example.com/v1/modules/"
#   }
#   cert = "/etc/ssl/private/tf-proxy-ca.pem"
# }
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Option D: serve *providers* from the same proxy — the provider network mirror
# protocol, which is separate from the module API above (both may be enabled at
# once). The URL is the mirror root; add a path only if the proxy runs with
# TFM_PROVIDER_MIRROR_PREFIX. `include` keeps everything else on the public
# registry, so a mirror can be rolled out one provider at a time.
#
# provider_installation {
#   network_mirror {
#     url     = ["https://tf.example.com/"]
#     include = ["registry.terraform.io/hashicorp/*"]
#   }
# }
# ---------------------------------------------------------------------------
