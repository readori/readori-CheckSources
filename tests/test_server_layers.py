def test_server_layer_boundaries_are_importable() -> None:
    from server.api import create_app
    from server.core import canonical_source_key, prepare_source_groups
    from server.worker import JobStore, ValidationService

    assert callable(create_app)
    assert callable(canonical_source_key)
    assert callable(prepare_source_groups)
    assert JobStore is not None
    assert ValidationService is not None
