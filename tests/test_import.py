from io import BytesIO
from openpyxl import Workbook
import pytest
from pica import extract_code, parse_pica
from commissioni import parse_commissioni


def make_excel(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def pica_fixture():
    return make_excel([
        ["univr 26-02 - Esportazione dati"],
        ["ID domanda", "Stato", "Corso di Studio", "sede",
         "Nome insegnamento padre", "Descrizione Copertura",
         "ID Copertura", "Cognome", "Nome", "Codice fiscale"],
        [1001, "presentata", "CDS DEMO", "SEDE A",
         "INSEGNAMENTO X", "MODULO X", 100, "Candidato", "Uno", "CODICEFISCALE"],
        [1001, "presentata", "CDS DEMO", "SEDE A",
         "INSEGNAMENTO Y", "MODULO Y", 101, "Candidato", "Uno", "CODICEFISCALE"],
        [1002, "presentata", "CDS DEMO", "SEDE A",
         "INSEGNAMENTO X", "MODULO X", 100, "Candidato", "Due", "CODICEFISCALE2"],
    ])


def commissioni_fixture():
    return make_excel([
        ["CORSO DI LAUREA", "SEDE", "PRESIDENTE", "COMPONENTI COMMISSIONE",
         "BANDO", "REQUISITI", "ANNO"],
        ["CDS DEMO", "SEDE A", "Presidente", "Componente",
         "Bando 26-02", "Testo prova", "ANNO DEMO"],
        ["CDS DEMO", "SEDE B", "Presidente", "Componente",
         "Bando 26-02", "Testo prova", "ANNO DEMO"],
    ])


def test_bando_code_from_first_row():
    assert extract_code("univr 26-02 - Esportazione dati") == "26-02"
    with pytest.raises(ValueError):
        extract_code("file di esempio senza codice")


def test_multiple_assignments_for_same_candidate():
    result = parse_pica("demo.xlsx", pica_fixture())
    assert result["code"] == "26-02"
    assert result["summary"]["candidature"] == 3
    assert result["summary"]["incarichi"] == 2
    assert result["summary"]["cds"] == 1
    assert result["rows"][0]["id_pica"] == result["rows"][1]["id_pica"]
    assert result["rows"][0]["incarico"]["key"] != result["rows"][1]["incarico"]["key"]


def test_commissions_per_cds_multiple_seats():
    result = parse_commissioni("commissioni.xlsx", commissioni_fixture(), "26-02")
    assert result["numero_cds"] == 1
    assert result["numero_sedi"] == 2
    with pytest.raises(ValueError):
        parse_commissioni("commissioni.xlsx", commissioni_fixture(), "26-03")


def test_web_import_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("RANKUP_DATA_DIR", str(tmp_path))
    import importlib
    import server
    importlib.reload(server)
    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        assert client.get("/api/health").status_code == 200
        preview = client.post("/api/pica/preview",
                              files={"file": ("demo.xlsx", pica_fixture(),
                                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert preview.status_code == 200, preview.text
        token = preview.json()["token"]
        confirm = client.post("/api/pica/commit", json={"token": token})
        assert confirm.status_code == 200, confirm.text
        assert client.get("/api/bandi").json()["bandi"][0]["codice"] == "26-02"
        assert len(client.get("/api/bandi/26-02/incarichi").json()["incarichi"]) == 2
        assert len(client.get("/api/bandi/26-02/candidature").json()["candidature"]) == 3
        up = client.post("/api/bandi/26-02/commissioni/import",
                         files={"file": ("demo.xlsx", commissioni_fixture(),
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert up.status_code == 200, up.text
        assert len(client.get("/api/bandi/26-02/commissioni").json()["commissioni"]) == 2
        repeat = client.post("/api/pica/preview",
                             files={"file": ("demo.xlsx", pica_fixture(),
                                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert repeat.status_code == 409
