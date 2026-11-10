"""Date PICA, scadenza bando, SSD e dismissione IA: dati esclusivamente sintetici."""
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from title_date_check import parse_pica_title_date, check_three_years
from pica import parse_pica
from test_pdf_import import _setup


def test_date_italiane_iso_e_seriale_excel():
    assert parse_pica_title_date("15 novembre 2012").isoformat() == "2012-11-15"
    assert parse_pica_title_date("01 dicembre 2023").isoformat() == "2023-12-01"
    assert parse_pica_title_date("2024-03-20 00:00:00").isoformat() == "2024-03-20"
    assert parse_pica_title_date("45261").isoformat() == "2023-12-01"
    assert parse_pica_title_date("31 febbraio 2023") is None


def test_three_years_exact_boundary_and_missing_deadline():
    deadline="2026-10-29T13:00"
    assert check_three_years("29 ottobre 2023",deadline)["stato"] == "OK"
    assert check_three_years("30 ottobre 2023",deadline)["stato"] == "ATTENZIONE"
    assert check_three_years("data inesistente",deadline)["stato"] == "DA VERIFICARE"
    assert check_three_years("29 ottobre 2023","")["stato"] == "DA VERIFICARE"
    assert check_three_years("29/02/2024","2027-02-28T12:00")["stato"] == "OK"


def _pica_example():
    book=Workbook()
    sheet=book.active
    # Intenzionalmente in ordine diverso dalle colonne O / AU / AW.
    sheet.append(["univr 26-02 - Esportazione dati"])
    sheet.append([
        "In data", "Diploma di Laurea/Laurea Specialistica/Laurea Magistrale in",
        "SSD", "ID domanda", "Corso di Studio", "Sede", "Nome insegnamento padre",
        "Descrizione Copertura", "Cognome", "Nome"
    ])
    sheet.append([
        "29 ottobre 2023", "Laurea dichiarata sintetica", "MEDS-01/A", 10001,
        "Corso sintetico", "Sede fittizia", "Insegnamento demo", "Modulo demo",
        "Candidato", "Test"
    ])
    out=BytesIO()
    book.save(out)
    return out.getvalue()


def test_pica_headers_are_identified_not_hardcoded_positions():
    parsed=parse_pica("fixture.xlsx",_pica_example())
    rec=parsed["rows"][0]
    assert rec["titolo_studio"] == "Laurea dichiarata sintetica"
    assert rec["data_conseguimento"] == "29 ottobre 2023"
    assert rec["incarico"]["ssd"] == "MEDS-01/A"


def test_reference_deadline_and_read_only_candidate_check(tmp_path, monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        url="/api/bandi/26-02/riferimenti"
        original=client.get(url)
        assert original.status_code==200
        ref=original.json()["riferimenti"]
        assert ref["data_scadenza"]==""
        data={key:ref.get(key,"") for key in
              ("repertorio","protocollo","data_avviso","anno_accademico")}
        data.update(data_scadenza="2026-10-29T13:00",expected_updated_at=ref["aggiornato_il"])
        result=client.put(url,headers={"X-RankUp-Request":"1"},json=data)
        assert result.status_code==200,result.text
        assert client.get(url).json()["riferimenti"]["data_scadenza"]=="2026-10-29T13:00"
        candidati=client.get("/api/bandi/26-02/candidature")
        assert candidati.status_code==200,candidati.text
        assert all("ssd" in x and "titolo_studio" in x and
                   "verifica_anzianita" in x for x in candidati.json()["candidature"])
        assert all(x["verifica_anzianita"]["stato"]=="DA VERIFICARE"
                   for x in candidati.json()["candidature"])
        invalid=client.put(url,headers={"X-RankUp-Request":"1"},
                           json=dict(data,data_scadenza="2026-02-30T13:00"))
        assert invalid.status_code==400
        assert client.get("/api/ai/ollama/status").status_code==404


def test_ui_presents_ssd_and_no_ai_controls():
    page=(Path(__file__).resolve().parents[1]/"rankup/app/static/index.html").read_text()
    assert "<strong>SSD:</strong>" in page
    assert 'r.titolo_studio' in page
    assert 'r.verifica_anzianita' in page
    assert 'data_scadenza' in page
    assert 'data-archive-ai' not in page
    assert "Verifica Ollama locale" not in page
