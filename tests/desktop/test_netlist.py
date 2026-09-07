from __future__ import annotations

from isaca_api.models import NormalizeRequest
from isaca_api.netlist import normalize_netlist


def test_element_first_line_gets_a_title() -> None:
    document = normalize_netlist(NormalizeRequest(netlist_text="R1 in 0 R value=1k"))
    assert document.netlist_text.startswith("\"Untitled circuit\"\nR1")
    assert document.netlist_text.rstrip().endswith(".end")


def test_existing_title_and_directives_are_preserved() -> None:
    source = '"RC lowpass"\nR1 in out R value=1k\n.source V1\n.detector V_out\n.end\n'
    document = normalize_netlist(NormalizeRequest(netlist_text=source))
    assert document.title == "RC lowpass"
    assert document.netlist_text == source


def test_explicit_default_mode_is_visible_in_diagnostics() -> None:
    source = "Test\nM1 d g s 0 M gm={gm}\n.end\n"
    document = normalize_netlist(
        NormalizeRequest(netlist_text=source, use_slicap_defaults=True)
    )
    assert any(item.code == "slicap_defaults_used" for item in document.diagnostics)
