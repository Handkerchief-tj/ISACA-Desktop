from __future__ import annotations

import pytest

from isaca_api.models import (
    AnalysisPorts,
    PinRef,
    Point,
    SchematicComponent,
    SchematicDocument,
    SchematicWire,
)


@pytest.fixture
def rc_schematic() -> SchematicDocument:
    components = [
        SchematicComponent(
            id="vin",
            refdes="V1",
            device="V",
            position=Point(x=0, y=0),
            model="V",
            parameters={"value": "Vin"},
        ),
        SchematicComponent(
            id="r1",
            refdes="R1",
            device="R",
            position=Point(x=0, y=80),
            model="R",
            parameters={"value": "R"},
        ),
        SchematicComponent(
            id="c1",
            refdes="C1",
            device="C",
            position=Point(x=70, y=120),
            model="C",
            parameters={"value": "C"},
        ),
        SchematicComponent(
            id="ground",
            refdes="GND1",
            device="GROUND",
            position=Point(x=0, y=170),
            properties={"name": "0"},
        ),
        SchematicComponent(
            id="input-port",
            refdes="PIN",
            device="PORT",
            position=Point(x=-60, y=-20),
            properties={"name": "in"},
        ),
        SchematicComponent(
            id="output-port",
            refdes="POUT",
            device="PORT",
            position=Point(x=120, y=100),
            properties={"name": "out"},
        ),
    ]
    pairs = [
        (("input-port", "port"), ("vin", "outp")),
        (("vin", "outp"), ("r1", "pos")),
        (("vin", "outn"), ("ground", "0")),
        (("r1", "neg"), ("c1", "pos")),
        (("r1", "neg"), ("output-port", "port")),
        (("c1", "neg"), ("ground", "0")),
    ]
    wires = [
        SchematicWire(
            id=f"W{index}",
            source=PinRef(component_id=source[0], pin_id=source[1]),
            target=PinRef(component_id=target[0], pin_id=target[1]),
        )
        for index, (source, target) in enumerate(pairs, start=1)
    ]
    return SchematicDocument(
        title="RC lowpass",
        components=components,
        wires=wires,
        parameters={"R": "1k", "C": "1u"},
        analysis=AnalysisPorts(source="V1", detector="V_out"),
    )
