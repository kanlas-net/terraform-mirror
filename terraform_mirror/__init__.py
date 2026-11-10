"""terraform-mirror: a caching mirror for the Terraform registry protocols.

It serves the module registry (registry protocol v1) and the provider network mirror from
its own storage, fetching from the upstream registry on a miss.

The package is dependency-free: it only uses the Python standard library so it can run
in a slim container without a build step.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
