"""Regressione visiva statica: griglia incarichi CO Veneto allineata."""
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "rankup" / "app" / "static"


def test_co_veneto_contract_grid_uses_shared_label_height_and_responsive_columns():
    javascript = (STATIC / "co_veneto_ui.js").read_text(encoding="utf-8")
    css = (STATIC / "workspace.css").read_text(encoding="utf-8")
    assert 'class="ru-co-contract-fields"' in javascript
    assert 'class="ru-co-contract-field"' in javascript
    assert "job.work[field[0]],fieldLocked,true" in javascript
    assert "minmax(min(100%,260px),1fr)" in css
    assert ".ru-co-contract-field > .caption" in css
    assert "min-height:2.8em" in css
    assert ".ru-co-contract-field > input" in css
    assert "margin-top:auto!important" in css
    assert 'data-co-field=' in javascript
