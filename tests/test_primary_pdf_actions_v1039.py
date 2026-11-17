"""Revisione visiva di due azioni primarie RankUP, senza modifiche funzionali."""
from pathlib import Path

STATIC=Path(__file__).resolve().parents[1]/"rankup"/"app"/"static"
HTML=(STATIC/"index.html").read_text(encoding="utf-8")
CSS=(STATIC/"workspace.css").read_text(encoding="utf-8")


def test_pdf_generation_card_has_only_one_primary_highlight():
    assert HTML.count('class="card ru-sheet-generate--primary"') == 1
    assert 'Genera schede e verbali descrittivi PDF editabili' in HTML
    assert 'id="sheet-download"' in HTML
    assert 'id="sheet-cds"' in HTML and 'id="sheet-sede"' in HTML
    assert '.ru-workspace .card.ru-sheet-generate--primary' in CSS
    assert 'html[data-theme="dark"] .ru-workspace .card.ru-sheet-generate--primary' in CSS
    assert '.ru-sheet-generate--primary #sheet-download' in CSS


def test_pdf_reimport_card_is_highlighted_independently():
    assert HTML.count('class="card ru-val-tool ru-val-tool--primary"') == 1
    assert 'Reimportazione schede PDF' in HTML
    assert 'id="pdf-eval-files"' in HTML and 'id="pdf-eval-preview"' in HTML
    assert '.ru-workspace .ru-val-page .card.ru-val-tool--primary' in CSS
    assert 'html[data-theme="dark"] .ru-workspace .ru-val-page .card.ru-val-tool--primary' in CSS
    assert '.ru-val-tool--primary #pdf-eval-preview' in CSS


def test_primary_highlights_use_different_colors_without_flow_changes():
    # La classe visiva si applica ai due contenitori, non ai blocchi di stato.
    assert 'ru-val-advance ru-val-tool--primary' not in HTML
    assert 'ru-val-filters ru-val-tool--primary' not in HTML
    assert 'ru-sheet-generate--primary ru-val-tool--primary' not in HTML
    assert '1.0.40' in HTML
