"""HTTP server and durable worker for Readori source validation.

The implementation is intentionally not imported eagerly. Keeping this
package lightweight avoids ``runpy`` warnings when launching
``python -m server.source_validator_server`` and lets the Windows GUI bundle
the existing validator without requiring FastAPI extras.
"""

__all__ = ["ServerSettings", "ValidationService", "create_app"]


def __getattr__(name: str):
    if name in __all__:
        from .source_validator_server import ServerSettings, ValidationService, create_app
        return {"ServerSettings": ServerSettings, "ValidationService": ValidationService, "create_app": create_app}[name]
    raise AttributeError(name)
