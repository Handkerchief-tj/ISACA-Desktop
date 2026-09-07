from __future__ import annotations

import pytest

from isaca_api.slicap_adapter import SLiCAPAdapterError, _make_circuit


class _UnknownModelSLiCAP:
    @staticmethod
    def makeCircuit(*args, **kwargs):
        raise KeyError(False)


class _OtherKeyErrorSLiCAP:
    @staticmethod
    def makeCircuit(*args, **kwargs):
        raise KeyError("unexpected")


def test_unknown_model_error_is_translated() -> None:
    with pytest.raises(SLiCAPAdapterError, match="undefined or invalid element model"):
        _make_circuit(_UnknownModelSLiCAP)


def test_unrelated_key_error_is_not_hidden() -> None:
    with pytest.raises(KeyError, match="unexpected"):
        _make_circuit(_OtherKeyErrorSLiCAP)
