from sfg_prototype import ensure_supported_runtime, runtime_info


def test_active_slicap_runtime_is_supported():
    info = ensure_supported_runtime()
    assert info.supported
    assert not info.missing_hooks
    assert info == runtime_info()
