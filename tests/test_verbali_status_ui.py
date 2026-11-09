"""Verifica delle indicazioni di stato nella pagina Verbali CdS (solo UI)."""
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PAGE=(ROOT/"rankup/app/static/index.html").read_text(encoding="utf-8")


def test_stato_in_lavorazione_ambra_in_entrambi_i_temi():
    assert 'ready?"Pronto":"In lavorazione"' in PAGE
    assert '"ru-status-working"' in PAGE
    assert '.ru-status-working{background:#f3c74f;color:#473300' in PAGE
    assert 'html[data-theme="dark"] .ru-status-working{background:#f3c74f;color:#473300' in PAGE
    assert '.ru-verb-pending{border-color:#d6a93d;background:#fff9e8}' in PAGE
    assert 'html[data-theme="dark"] .ru-verb-pending{background:#332a19;border-color:#b98c28}' in PAGE


def test_definitiva_con_coccarda_non_solo_colore():
    assert '<symbol id="ri-rosette"' in PAGE
    assert 'var statusClass=approved?"ru-status-approved":final?"ru-status-final":' in PAGE
    assert 'var rosette=final?' in PAGE
    assert 'class="ru-status-rosette"' in PAGE
    assert '<use href="#ri-rosette"></use>' in PAGE
    assert "rosette+esc(label)" in PAGE
    assert '.ru-status-final{background:#237749;color:#fff' in PAGE
    assert 'html[data-theme="dark"] .ru-status-final{background:#237749;color:#fff' in PAGE


def test_pronto_ed_approvato_resta_distinti():
    assert 'cardClass=(approved||final)?"ru-verb-ready":ready?"ru-verb-pronto":"ru-verb-pending"' in PAGE
    assert 'statusClass=approved?"ru-status-approved":final?"ru-status-final":ready?"ru-status-ready":"ru-status-working"' in PAGE
    assert 'label=approved?"Atti approvati":final?"Definitiva":ready?"Pronto":"In lavorazione"' in PAGE
    assert '.ru-verb-pronto{border-color:#5b95c9;background:#f3f8fd}' in PAGE
