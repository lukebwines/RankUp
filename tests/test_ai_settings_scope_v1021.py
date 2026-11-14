"""Regressione interfaccia Impostazioni AI: callback fuori dal blocco try.

In JS, le function declaration dentro un blocco try possono essere block-scoped,
mentre i gestori dei pulsanti sono registrati dopo il blocco.
I due helper devono quindi essere nello scope della funzione di rendering.
"""
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "rankup" / "app" / "static" / "index.html"


def test_ai_settings_helpers_are_visible_to_handlers_after_try():
    script = UI.read_text(encoding="utf-8")
    start = script.index("async function renderAISettings(){")
    end = script.index("async function renderAIBando(){", start)
    screen = script[start:end]

    # Assegnazioni 'var' nello scope di renderAISettings (non function
    # declaration locali al blocco try, responsabili del ReferenceError).
    assert "var fillModels=function(items){" in screen
    assert "var loadModelLimits=async function(model){" in screen
    assert "function fillModels(items){" not in screen.replace("var fillModels=function(items){", "")
    assert "async function loadModelLimits(model){" not in screen.replace(
        "var loadModelLimits=async function(model){", "")

    # Entrambi gli helper sono richiamati da handler registrati dopo il catch.
    assert screen.index("}catch(err){note(err.message,true);}\n        $(\"ai-config\").onsubmit") > screen.index(
        "var loadModelLimits=async function(model){")
    assert "fillModels(items);" in screen
    assert "await loadModelLimits(selected);" in screen


def test_addon_version_tracks_frontend_fix():
    config = (UI.parents[2] / "config.yaml").read_text(encoding="utf-8")
    import re
    match = re.search(r'^version:\s*"(\d+)\.(\d+)\.(\d+)"', config, re.M)
    assert match and tuple(map(int,match.groups())) >= (1,0,21)
