"""Regressione cache CSS e dimensione checkbox - RankUP v1.0.23."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "rankup" / "app" / "static" / "index.html"
CONFIG = ROOT / "rankup" / "config.yaml"
CSS = ROOT / "rankup" / "app" / "static" / "workspace.css"


def test_css_cache_key_matches_addon_version():
    html = INDEX.read_text(encoding="utf-8")
    config = CONFIG.read_text(encoding="utf-8")
    version = re.search(r'^version:\\s*"([^"]+)"', config, flags=re.M)
    assert version
    assert f'href="static/workspace.css?v={version.group(1)}"' in html


def test_ai_selection_checkbox_is_not_expanded_by_old_table_rules():
    html = INDEX.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert '<col style="width:68px">' in html
    assert '<td class="ru-ai-select-cell"><span class="ru-ai-select-inner">' in html
    assert 'style="width:19px!important;min-width:19px!important;max-width:19px!important;' in html
    assert 'height:19px!important;min-height:19px!important;max-height:19px!important;margin:0!important"' in html
    assert '.ru-workspace .tablebox input:not([type="checkbox"]):not([type="radio"])' in css
