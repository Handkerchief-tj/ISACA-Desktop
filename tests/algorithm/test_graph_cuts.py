"""Focused tests for paper Section IV-B complementary graph cuts."""

from sfg_prototype.analysis import _enumerate_graph_cuts


def _reverse(adjacency: dict[str, set[str]]) -> dict[str, set[str]]:
    reverse = {vertex: set() for vertex in adjacency}
    for source, targets in adjacency.items():
        for target in targets:
            reverse.setdefault(target, set()).add(source)
    return reverse


def _edge_lookup(adjacency: dict[str, set[str]]) -> dict[tuple[str, str], tuple[tuple[str, str, str], ...]]:
    return {
        (source, target): ((source, target, "test"),)
        for source, targets in adjacency.items()
        for target in targets
    }


def test_complementary_cut_detects_single_entry_single_exit_diamond() -> None:
    adjacency = {
        "pre": {"a"},
        "a": {"upper", "lower"},
        "upper": {"b"},
        "lower": {"b"},
        "b": {"post"},
        "post": set(),
    }

    cuts = _enumerate_graph_cuts(
        adjacency,
        _reverse(adjacency),
        _edge_lookup(adjacency),
        max_cuts=32,
        include_fallback=False,
    )

    cut = next(item for item in cuts if item.start_vertex == "a" and item.end_vertex == "b")
    assert cut.cut_kind == "complementary"
    assert cut.vertices == frozenset({"a", "upper", "lower", "b"})
    assert cut.length == 2


def test_complementary_cut_rejects_an_internal_escape_path() -> None:
    adjacency = {
        "pre": {"a"},
        "a": {"upper", "lower"},
        "upper": {"b", "escape"},
        "lower": {"b"},
        "b": {"post"},
        "escape": {"post"},
        "post": set(),
    }

    cuts = _enumerate_graph_cuts(
        adjacency,
        _reverse(adjacency),
        _edge_lookup(adjacency),
        max_cuts=32,
        include_fallback=False,
    )

    assert not any(item.start_vertex == "a" and item.end_vertex == "b" for item in cuts)


def test_complementary_cut_ignores_a_single_nonreconvergent_edge() -> None:
    adjacency = {"pre": {"a"}, "a": {"b"}, "b": {"post"}, "post": set()}

    cuts = _enumerate_graph_cuts(
        adjacency,
        _reverse(adjacency),
        _edge_lookup(adjacency),
        max_cuts=32,
        include_fallback=False,
    )

    assert not any(item.start_vertex == "a" and item.end_vertex == "b" for item in cuts)
