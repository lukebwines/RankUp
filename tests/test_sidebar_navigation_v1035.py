"""Regressioni menu laterale RankUP 1.0.42 (sole modifiche di navigazione)."""
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "rankup/app/static/index.html"


def _source_and_nav():
    source = INDEX.read_text(encoding="utf-8")
    nav = source.split('<nav id="nav">', 1)[1].split("</nav>", 1)[0]
    return source, nav


def test_sidebar_has_three_logical_groups_in_the_agreed_order():
    _, nav = _source_and_nav()
    expected = [
        "GESTIONE GENERALE",
        'data-page="dashboard"',
        'data-page="profilo"',
        "CONFIGURAZIONE OPERATIVA",
        'data-page="commissioniarchivio"',
        'data-page="ssdconfig"',
        "AMMINISTRAZIONE",
        'data-page="utenti"',
        'data-page="assistenteai"',
        'data-page="audit"',
        'data-page="diagnostica"',
        'data-page="azzeramento"',
        "PROCEDIMENTO DEL BANDO ATTIVO",
    ]
    offsets = [nav.index(token) for token in expected]
    assert offsets == sorted(offsets)
    for token in expected[1:-1]:
        assert nav.count(token) == 1


def test_sidebar_preserves_existing_permissions_for_operators():
    source, nav = _source_and_nav()
    assert 'id="admin-commissioni-link"' in nav
    assert '$("admin-commissioni-link").style.display="flex";' in source
    assert 'id="admin-ssd-link"' in nav
    assert '$("admin-ssd-link").style.display=state.role==="admin"?"flex":"none";' in source
    assert 'id="operator-profile-link"' in nav
    assert '$("operator-profile-link").style.display=state.role==="user"?"flex":"none";' in source
    for name in ("users", "audit", "diagnostica", "ai", "reset"):
        assert 'id="admin-' + name + '-link"' in nav
    assert '$("admin-menu-heading").style.display=state.role==="admin"?"block":"none";' in source


def test_release_metadata_is_consistent():
    source, _ = _source_and_nav()
    config = (INDEX.parents[2] / "config.yaml").read_text(encoding="utf-8")
    assert 'version: "1.0.42"' in config
    assert "RANKUP · v1.0.42" in source
    assert "operativo.css?v=1.0.42" in source
    assert "workspace.css?v=1.0.42" in source
