from __future__ import annotations

import time

from fastapi.testclient import TestClient

from isaca_api.app import create_app


def test_health_catalog_and_conversion(tmp_path, rc_schematic) -> None:
    with TestClient(create_app(tmp_path / "runs")) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["slicap"] == "5.2.1"

        catalog = client.get("/api/v1/catalog/devices").json()["devices"]
        assert {"R", "C", "L", "V", "I", "G", "E", "F", "H", "M", "QV"}.issubset(catalog)
        assert catalog["G"]["symbol"] == "VCCS"
        assert catalog["G"]["pins"] == ["outp", "outn", "inp", "inn"]
        assert catalog["G"]["pin_positions"]["inp"] == {"x": -20.0, "y": -20.0}

        symbol = client.get("/api/v1/catalog/symbols/VCCS.svg")
        assert symbol.status_code == 200
        assert symbol.headers["content-type"].startswith("image/svg+xml")
        assert b"<svg" in symbol.content

        bundle = client.get("/api/v1/catalog/symbols/core")
        assert bundle.status_code == 200
        assert b"<defs>" in bundle.content
        assert b'<g id="R"' in bundle.content

        response = client.post(
            "/api/v1/schematics/convert",
            json={"schematic": rc_schematic.model_dump(mode="json"), "output_format": "cir"},
        )
        assert response.status_code == 200
        assert "R1 in out R value={R}" in response.json()["netlist_text"]


def test_official_exporter_reports_unresolved_component_value(tmp_path, rc_schematic) -> None:
    broken = rc_schematic.model_copy(deep=True)
    resistor = next(component for component in broken.components if component.refdes == "R1")
    resistor.parameters["value"] = "?"
    with TestClient(create_app(tmp_path / "runs")) as client:
        response = client.post(
            "/api/v1/schematics/convert",
            json={"schematic": broken.model_dump(mode="json"), "output_format": "cir"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["netlist_text"] is None
        assert any(
            item["code"] == "official_missing_value"
            for item in payload["diagnostics"]
        )


def test_numeric_analysis_job_completes_through_http_api(tmp_path, rc_schematic) -> None:
    with TestClient(create_app(tmp_path / "runs")) as client:
        converted = client.post(
            "/api/v1/schematics/convert",
            json={"schematic": rc_schematic.model_dump(mode="json"), "output_format": "cir"},
        ).json()
        submitted = client.post(
            "/api/v1/analyses",
            json={
                "netlist_text": converted["netlist_text"],
                "modes": ["laplace", "pz"],
                "numeric": True,
            },
        )
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]

        state = submitted.json()
        deadline = time.monotonic() + 30
        while state["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.1)
            state = client.get(f"/api/v1/analyses/{job_id}").json()

        assert state["status"] == "completed", state.get("error")
        analyses = state["result"]["analyses"]
        assert "laplace" in analyses
        assert len(analyses["pz"]["poles"]) == 1
        artifact = client.get(f"/api/v1/analyses/{job_id}/artifacts/result.json")
        assert artifact.status_code == 200


def test_chinese_utf8_netlist_is_transcoded_for_windows_slicap(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "runs")) as client:
        submitted = client.post(
            "/api/v1/analyses",
            json={
                "netlist_text": "中文标题\n* 中文注释\nR1 in 0 R value=1k\n.end\n",
                "modes": [],
                "numeric": False,
            },
        )
        job_id = submitted.json()["id"]
        state = submitted.json()
        deadline = time.monotonic() + 30
        while state["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.1)
            state = client.get(f"/api/v1/analyses/{job_id}").json()
        assert state["status"] == "completed", state.get("error")
        software = state["result"]["software"]
        assert software["transcoding_replacements"] == 0
        assert software["title_sanitized"] is True
        assert state["result"]["circuit"]["title"] == "中文标题"
