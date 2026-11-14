"""Regressione UI: checkbox nella colonna Selez. della verifica requisito ente.

Controllo statico della struttura e delle regole CSS. I dati PICA non sono usati.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "rankup" / "app" / "static" / "index.html"
CSS = ROOT / "rankup" / "app" / "static" / "workspace.css"


def test_selection_column_has_fixed_width_and_checkbox_wrapper():
    html = HTML.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert '<col style="width:68px"><col style="width:13%">' in html
    assert '<th class="ru-ai-select-head"' in html
    assert '<td class="ru-ai-select-cell"><span class="ru-ai-select-inner">' in html
    assert '.ru-workspace .ru-ai-select-inner{' in css
    assert 'display:flex;justify-content:center;align-items:center;width:100%;min-width:0;' in css
    assert 'min-width:19px!important;max-width:19px!important;' in css


def test_generic_table_input_min_width_does_not_affect_checkboxes():
    css = CSS.read_text(encoding="utf-8")
    assert '.ru-workspace .tablebox input:not([type="checkbox"]):not([type="radio"]),.ru-workspace .tablebox select{min-width:115px}' in css
    assert '.ru-workspace .tablebox input,.ru-workspace .tablebox select{min-width:115px}' not in css


def test_release_metadata():
    config = (ROOT / "rankup" / "config.yaml").read_text(encoding="utf-8")
    import re
    match = re.search(r'^version:\s*"(\d+)\.(\d+)\.(\d+)"', config, re.M)
    assert match and tuple(map(int,match.groups())) >= (1,0,22)
