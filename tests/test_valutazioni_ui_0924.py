"""Controlli di regressione sul frontend Valutazioni 0.9.24.

Il restyling non deve sostituire i controlli di acquisizione o modificare
le API per candidature, valutazioni, verbali e graduatorie.
"""
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
HTML=(ROOT/"rankup/app/static/index.html").read_text(encoding="utf-8")
CSS=(ROOT/"rankup/app/static/valutazioni.css").read_text(encoding="utf-8")


def test_foglio_stile_isolato_e_compatibile_con_i_due_temi():
    assert '<link rel="stylesheet" href="static/valutazioni.css">' in HTML
    assert 'html[data-theme="dark"] .ru-val-page' in CSS
    assert '.ru-val-page .ru-val-advance-table' in CSS
    assert '@media (max-width: 560px)' in CSS
    assert '@media (prefers-reduced-motion: reduce)' in CSS


def test_controlli_operativi_valutazioni_presenti_dopo_restyling():
    # Unico renderer della pagina: preservare le azioni e i relativi ID.
    assert HTML.count('async function renderValutazioni(){')==1
    assert HTML.count('id="pdf-eval-files"')==1
    assert HTML.count('id="pdf-eval-preview"')==1
    assert HTML.count('id="pdf-eval-result"')==1
    assert 'function renderPdfPreview(code,data)' in HTML
    assert 'async function renderManualEvaluation(code, record, manualScope)' in HTML
    assert 'data-archive-ai' in HTML
    assert 'data-val-manual' in HTML
    assert 'data-val-course' in HTML
    assert 'id="val-view-current"' in HTML
    assert 'id="val-view-all"' in HTML


def test_riepiloghi_derivano_da_api_e_non_da_numeri_di_esempio():
    assert 'var expectedTotal=Number(report.totale_attese)||0' in HTML
    assert 'var acquiredTotal=Number(report.totale_acquisite)||0' in HTML
    assert 'var outstanding=Math.max(0,expectedTotal-acquiredTotal)' in HTML
    assert 'var split=schedeDaMostrare(archive)' in HTML
    assert 'var tieByArea=new Map()' in HTML
    assert 'tieCandidates>0' in HTML
    assert 'Parità da verificare' in HTML
    assert 'var completion=expectedTotal?' in HTML


def test_filtri_e_navigazione_restano_operativi():
    for token in (
        'id="val-cds"', 'id="val-sede"', 'id="val-reset-filters"',
        'id="val-table-search"', 'id="val-table-pending"',
        'function applyLocalValFilters()', 'state.valJumpToGroup=true',
        'id="val-groups"', 'class="ru-val-group"', 'class="ru-val-job"',
        'role="progressbar"'
    ):
        assert token in HTML, token
    assert "state.valTableQuery" in HTML
    assert "state.valTableFocusOnly" in HTML


def test_altri_moduli_non_sono_stati_eliminati():
    for expected in (
        'async function renderVerbaliGraduatorie()',
        'async function renderApprovazioniGraduatorie()',
        'async function renderValutazioni()',
        'async function renderList(page)',
        'function renderPdfPreview(code,data)',
        'async function renderSchede()',
        'async function renderCommissioni',
        'async function renderArchivioFinale',
    ):
        assert expected in HTML, expected
