"""Keep nginx-fronted debug ports restricted to the local host.

The test venv has no PyYAML. Read only the existing inline-list syntax, bounded
to one named service block, and fail if the format becomes ambiguous.
"""

import ast
import re
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.prod.yml"


@pytest.mark.parametrize("service, port", [("backend", 8000), ("fgt_dashboard", 8050)])
def test_debug_host_port_is_loopback_only(service, port):
    document = COMPOSE.read_text(encoding="utf-8")
    blocks = re.findall(
        rf"(?ms)^  {re.escape(service)}:[ \t]*\n(.*?)(?=^  [\w-]+:|\Z)", document
    )
    assert len(blocks) == 1, f"Expected one {service} service block"
    declarations = re.findall(
        r"(?m)^    ports:[ \t]*(\[[^\r\n]*\])[ \t]*(?:#.*)?$", blocks[0]
    )
    assert len(declarations) == 1, "Expected one unambiguous inline ports list"
    ports = ast.literal_eval(declarations[0])
    assert isinstance(ports, list)
    assert ports == [f"127.0.0.1:{port}:{port}"]
