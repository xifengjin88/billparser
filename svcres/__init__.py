"""Cloud service name resolution: product codes and messy labels -> canonical services."""

from .catalog import Catalog, build
from .resolver import Resolution, Resolver

__all__ = ["Catalog", "Resolution", "Resolver", "build"]
