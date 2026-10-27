"""The Data Center: buckets of data (managed folders, linked local folders, read-only S3 links), their files, tags
and detected formats. `service` is the only entry point (contract: API.md); see README.md."""

from .store import DCError

__all__ = ['DCError']
