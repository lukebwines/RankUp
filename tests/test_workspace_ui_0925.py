"""Regressioni sul restyling globale RankUP 0.9.25.

La suite verifica l'inclusione di tutti i pannelli e la conservazione dei
controlli amministrativi: il restyling non interviene sulle API e sul database.
"""
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
PAGE=(ROOT/"rankup/app/static/index.html").read_text(encoding="utf-8")
CSS=(ROOT/"rankup/app/static/workspace.css").read_text(encoding="utf-8")

PAGES=[
    "dashboard","riferimenti","incarichi","candidature","commissioni","schede",
    "verbali","graduatorie","archivio","utenti","ssdconfig",
    "commissioniarchivio","azzeramento",
]


def test_tutte_le_sezioni_presenti_e_guide_procedurali():
    for page in PAGES:
        assert f'{page}:' in PAGE or f'"{page}"' in PAGE, page
        assert f'data-page="{page}"' in PAGE, page
    for x in ("RU_PROCESS","RU_DESCRIPTIONS","rankupWorkspaceGuide",
              "ru-stage-banner","data-ru-go"):
        assert x in PAGE
    assert "go(page)" in PAGE


def test_layout_di_tutte_le_sezioni_isolato_dalle_valutazioni():
    assert '<link rel="stylesheet" href="static/workspace.css?v=1.0.9">' in PAGE
    assert '<link rel="stylesheet" href="static/valutazioni.css">' in PAGE
    assert 'if(state.page==="valutazioni"||!content)' in PAGE
    assert 'ru-workspace ru-page-' in PAGE
    assert 'html(\'<div class="ru-val-page">\'+content+\'</div>\')' in PAGE
    assert ':root {' in CSS and 'html[data-theme="dark"] {' in CSS
    for selector in (
        ".sidebar nav button", ".ru-workspace .card",
        ".ru-workspace .tablebox", ".ru-workspace .btn",
        ".ru-workspace .ru-stage-banner", ".ru-approval-choice",
        ".ru-workspace .ru-admin-danger", ".ru-workspace .ru-verb-card",
        ".ru-workspace .ru-archive-warning",
    ):
        assert selector in CSS, selector


def test_riduzione_del_rischio_nelle_tabelle_interattive():
    for item in (
        'function enhanceWorkspaceTables()',
        '["incarichi","candidature","valutazioni"].includes(state.page)',
        'tbody input[type=',
        'tbody details',
        "tbody input,tbody select,tbody textarea",
        'data-ru-go',
        "box.dataset.ruSearchReady",
        'aria-label","Cerca tra le righe della tabella"',
    ):
        assert item in PAGE, item
    assert ".ru-workspace .tablebox tbody tr[hidden]" in CSS


def test_ricerca_incarichi_inclusi_dettagli_e_contatori():
    for item in (
        'id="incarichi-search"', 'class="ru-incarico-parent"',
        'class="ru-incarico-details"', 'function filtraIncarichi()',
        'id="incarichi-visible-counter"', 'id="incarichi-empty"',
        'state.incarichiQuery', 'row.nextElementSibling',
    ):
        assert item in PAGE, item


def test_filtro_candidature_mantiene_selezione_alla_registrazione():
    for item in (
        'id="cand-search"', 'id="cand-stato"',
        'state.candidatureFilter={cds:course,ente:status',
        'state.candidatureFilter.cds||""',
        'state.candidatureFilter.ente||""',
        "Nessuna candidatura corrisponde ai filtri",
        'data-cand-save', 'data-cand-history',
    ):
        assert item in PAGE, item
    assert '((r.verifica_ente||{}).stato||"").indexOf(status)' in PAGE


def test_stati_documentali_e_avvisi_non_autogenerano_atti():
    for item in (
        'data-verb-create','data-verb-regress','data-verb-recover',
        'data-tie-open','data-tie-save','data-grad-select',
        'data-grad-approve','data-grad-regress',
        'id="archive-close-bando"','id="archive-candidate-search"',
        'data-pdf-download',
        'ru-quick-stats','ru-approval-card','ru-archive-warning',
    ):
        assert item in PAGE, item
    assert 'state.role==="admin"' in PAGE
    assert 'ru-admin-danger' in PAGE


def test_admin_e_generazione_schede_non_perdono_funzionalita():
    for item in (
        "renderUtenti","renderSSDGenerali","renderCommissioniGlobali",
        'id="new-user-form"','id="global-ssd-form"',
        'id="global-commission-reuse"','id="reset-ack"',
        'id="commission-import"','id="cm-manual-form"',
        'id="sheet-download"','id="refs-form"','id="pica-file"',
        'id="preview-btn"','id="pdf-eval-preview"',
    ):
        assert item in PAGE, item


def test_accessibilita_responsive_e_dark_mode():
    for required in (
        "@media(max-width:690px)", "@media(max-width:1100px)",
        "@media(prefers-reduced-motion:reduce)", ":focus-visible",
        ".ru-table-empty", ".ru-workspace .ru-status-pill",
    ):
        assert required in CSS, required
    assert 'n.setAttribute("aria-current","page")' in PAGE


def test_sqlite_e_api_non_richiesti_dal_restyling():
    assert "async function renderValutazioni()" in PAGE
    assert "async function renderArchivioFinale()" in PAGE
    assert "async function renderBandoReferences()" in PAGE
    assert "async function renderCommissioni(" in PAGE


def test_approvazioni_regredite_distinte_dalle_attive_e_storico_consultabile():
    # Stato storico derivato esclusivamente dall'API, senza conteggiarlo
    # come approvazione attiva e senza mutare gli atti.
    for marker in (
        'var historical=all.filter(function(x){return x.stato==="regredita";}).length',
        'approvazioni attive',
        'approvazioni regredite · storico',
        'Approvazioni attive e storico',
        'var isHistorical=a.stato==="regredita",isActive=a.stato==="approvata"',
        'ru-approval-card--historical',
        'data-approval-history="true"',
        'REGREDITA · STORICO',
        'Approvazione non più attiva, conservata esclusivamente',
        'Scarica PDF storico',
        'Apri PDF storico nel browser',
    ):
        assert marker in PAGE, marker
    assert 'a.stato==="bozza"' in PAGE
    assert 'a.stato==="approvata"' in PAGE
    assert 'state.role==="admin"&&a.stato==="approvata"' in PAGE
    assert 'state.role==="admin"&&a.stato==="bozza"' in PAGE
    assert 'data-grad-approve' in PAGE and 'data-grad-regress' in PAGE
    assert 'data-pdf-download' in PAGE
    # La regressione non può diventare un secondo procedimento operativo.
    assert "state.role===\"admin\"&&a.stato===\"regredita\"" not in PAGE


def test_approvazioni_regredite_grigie_ma_link_pdf_accessibili_due_temi():
    for selector in (
        '.ru-workspace .ru-approval-card--historical',
        '.ru-workspace .ru-approval-status-badge',
        '.ru-workspace .ru-status-pill--historical',
        'html[data-theme="dark"] .ru-workspace .ru-approval-card--historical',
        '.ru-workspace .ru-approval-card--historical .actions a.btn',
    ):
        assert selector in CSS, selector
    assert 'opacity:' not in CSS.split('/* Stato cromatico delle approvazioni:',1)[1].split(
        '.ru-approval-card > .caption',1)[0]


def test_status_cards_badges_are_unique_and_have_strong_colors():
    for text in (
        "var status=isHistorical?",
        "ATTI APPROVATI",
        "REGREDITA · STORICO",
        "BOZZA DA APPROVARE",
        "ru-approval-heading",
        "ru-approval-card--active",
    ):
        assert text in PAGE, text
    assert "Approvazione regredita · storico" not in PAGE
    assert ".ru-workspace .card.ru-approval-card--historical" in CSS
    assert ".ru-workspace .card.ru-approval-card--active" in CSS
    assert 'html[data-theme="dark"] .ru-workspace .card.ru-approval-card--historical' in CSS
    assert 'html[data-theme="dark"] .ru-workspace .card.ru-approval-card--active' in CSS


def test_nn_assignment_choices_visible_in_schede_and_valutazioni():
    for marker in (
        "function nnModelliEditor(rows)",
        "function collegaModelliNN(code,rerender)",
        "SSD NN · modello scheda per incarico",
        "data-nn-type",
        "data-nn-ref",
        "data-nn-save",
        "nnModelliEditor(nnRows)",
        "collegaModelliNN(code,renderSchede)",
        "collegaModelliNN(code,renderValutazioni)",
        "/incarichi/modelli-nn",
        "/modello-scheda",
    ):
        assert marker in PAGE, marker
    assert "NN — modello ordinario" not in PAGE
    assert "ru-nn-model-row" in CSS

def test_v1_footer_and_reset_preservation_notice():
    for marker in (
        "RANKUP · v1.0.9",
        "Crafted by Luca B.",
        "© 2026 Luca B. · All rights reserved.",
        "ru-footer-credit",
        "restano i modelli SSD permanenti",
        "non restano le associazioni delle commissioni",
    ):
        assert marker in PAGE, marker
    assert ".sidebar-footer .ru-footer-credit" in CSS
    assert 'id="logout-btn"' in PAGE
    assert 'id="account-info"' in PAGE


def test_verbali_stato_allineato_con_titoli_lunghi():
    # Colonne distinte impediscono che il badge scenda a sinistra quando
    # il nome ufficiale del CdS occupa più righe.
    assert 'ru-verb-summary' in PAGE
    assert 'ru-verb-status' in PAGE
    assert 'data-verb-create' in PAGE
    assert 'data-verb-regress' in PAGE
    assert 'display:grid!important' in CSS.split(
        '.ru-workspace .ru-verb-card .ru-verb-summary{',1)[1].split('}',1)[0]
    assert 'grid-template-columns:minmax(0,1fr) max-content' in CSS
    assert '.ru-workspace .ru-verb-card .ru-verb-summary > div{min-width:0}' in CSS
    assert 'justify-self:end' in CSS
    assert '@media(max-width:560px)' in CSS
