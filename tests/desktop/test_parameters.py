from __future__ import annotations

import pytest

from isaca_api.models import ParameterSource
from isaca_api.parameters import collect_parameter_specs, expand_scale_factors, numeric_substitutions


def test_engineering_suffixes_and_scientific_notation() -> None:
    assert float(eval(expand_scale_factors("2k"))) == pytest.approx(2e3)
    assert float(eval(expand_scale_factors("3m"))) == pytest.approx(3e-3)
    assert float(eval(expand_scale_factors("4u"))) == pytest.approx(4e-6)
    assert float(eval(expand_scale_factors("5n"))) == pytest.approx(5e-9)
    assert float(eval(expand_scale_factors("6p"))) == pytest.approx(6e-12)
    assert float(eval(expand_scale_factors("7e-9"))) == pytest.approx(7e-9)


def test_parameter_provenance_does_not_require_source_amplitude() -> None:
    netlist = """RC
V1 in 0 V value={Vin}
R1 in out R value=1k
C1 out 0 C value={C}
.param C=1u
.source V1
.detector V_out
.end
"""
    specs = collect_parameter_specs(netlist)
    by_name = {spec.name: spec for spec in specs}
    assert by_name["C"].source == ParameterSource.NETLIST
    assert by_name["C"].numeric_value == pytest.approx(1e-6)
    assert by_name["R1.value"].source == ParameterSource.INLINE
    assert by_name["R1.value"].numeric_value == pytest.approx(1e3)
    assert by_name["Vin"].required_for_numeric is False
    substitutions, missing = numeric_substitutions(specs)
    assert substitutions == {"C": pytest.approx(1e-6)}
    assert missing == []


def test_unresolved_component_parameter_is_reported() -> None:
    specs = collect_parameter_specs("Test\nG1 out 0 in 0 G value={gm}\n.end")
    substitutions, missing = numeric_substitutions(specs)
    assert substitutions == {}
    assert missing == ["gm"]


def test_slicap_defaults_are_versioned_and_opt_in() -> None:
    netlist = "Test\nM1 d g s 0 M gm={gm} cgs={cgs}\n.end"
    strict = {spec.name: spec for spec in collect_parameter_specs(netlist)}
    assert strict["gm"].source == ParameterSource.UNRESOLVED
    assert strict["cgs"].source == ParameterSource.UNRESOLVED

    opted_in = {
        spec.name: spec
        for spec in collect_parameter_specs(netlist, use_slicap_defaults=True)
    }
    assert opted_in["gm"].source == ParameterSource.SLICAP_DEFAULT
    assert opted_in["gm"].numeric_value == pytest.approx(1e-3)
    assert opted_in["cgs"].numeric_value == pytest.approx(0.0)
    assert opted_in["gm"].slicap_version == "5.2.1"


def test_param_continuation_lines_are_resolved() -> None:
    netlist = """Continued parameters
.param R=1k
+ C=100n gm=2m
R1 in out R value={R}
C1 out 0 C value={C}
G1 out 0 in 0 G value={gm}
.end
"""
    specs = collect_parameter_specs(netlist)
    substitutions, missing = numeric_substitutions(specs)
    assert missing == []
    assert substitutions["R"] == pytest.approx(1e3)
    assert substitutions["C"] == pytest.approx(1e-7)
    assert substitutions["gm"] == pytest.approx(2e-3)
