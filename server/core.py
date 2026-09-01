"""Core boundary for source identity, grouping, redaction and device gates.

These functions are kept as a small import surface for future worker/API
process separation while sharing the exact implementation used by the
single-process development entry point.
"""

from .source_validator_server import (
    canonical_source_key,
    canonical_source_site_key,
    detail_is_device_compatible,
    prepare_source_groups,
    redact,
    source_domain,
    source_rule_fingerprint,
)

__all__ = [
    "canonical_source_key",
    "canonical_source_site_key",
    "detail_is_device_compatible",
    "prepare_source_groups",
    "redact",
    "source_domain",
    "source_rule_fingerprint",
]
