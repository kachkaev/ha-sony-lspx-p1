"""quality_scale.yaml has the shape Home Assistant's hassfest expects (it only checks core integrations)."""

from __future__ import annotations

from pathlib import Path

import yaml

QUALITY_SCALE = Path(__file__).resolve().parent.parent / "custom_components" / "sony_lspx_p1" / "quality_scale.yaml"
RULE_COUNT = 54  # Bronze 20, Silver 10, Gold 21, Platinum 3


def test_every_rule_has_a_status_and_reasons_where_needed() -> None:
    rules = yaml.safe_load(QUALITY_SCALE.read_text())["rules"]
    assert len(rules) == RULE_COUNT
    for name, value in rules.items():
        if isinstance(value, str):
            assert value in {"done", "todo"}, name
            continue
        assert value.keys() == {"status", "comment"}, name
        assert value["status"] in {"done", "todo", "exempt"}, name
        assert value["comment"].strip(), name
