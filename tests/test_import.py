from io import BytesIO
import json
import zipfile
from pypdf import PdfReader
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
         "ID Copertura", "Cognome", "Nome", "Codice fiscale", "ssd", "Settore scientifico disciplinare"],
        [1001, "presentata", "CDS DEMO", "SEDE A",
         "INSEGNAMENTO X", "MODULO X", 100, "Candidato", "Uno", "CODICEFISCALE", "SSD-A", ""],
        [1001, "presentata", "CDS DEMO", "SEDE A",
         "INSEGNAMENTO Y", "MODULO Y", 101, "Candidato", "Uno", "CODICEFISCALE", "SSD-B", ""],
        [1002, "presentata", "CDS DEMO", "SEDE A",
         "INSEGNAMENTO X", "MODULO X", 100, "Candidato", "Due", "CODICEFISCALE2", "SSD-A", "SSD-PERSONALE"],
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
    assert result["summary"]["ssd_distinti"] == 2
    assert [x["incarico"]["ssd"] for x in result["rows"]] == ["SSD-A", "SSD-B", "SSD-A"]
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
    (tmp_path / "options.json").write_text(
        json.dumps({"admin_username": "test-admin",
                    "admin_password": "UnPasswordDiTestMoltoLungo123!"}),
        encoding="utf-8",
    )
    import importlib
    import server
    importlib.reload(server)
    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/bandi").status_code == 401
        invalid = client.post("/api/auth/login",
                              headers={"X-RankUp-Request": "1"},
                              json={"username": "test-admin", "password": "errata"})
        assert invalid.status_code == 401
        no_csrf = client.post("/api/auth/login", json={"username": "test-admin",
                                                       "password": "UnPasswordDiTestMoltoLungo123!"})
        assert no_csrf.status_code == 403
        login = client.post("/api/auth/login",
                            headers={"X-RankUp-Request": "1"},
                            json={"username": "test-admin",
                                  "password": "UnPasswordDiTestMoltoLungo123!"})
        assert login.status_code == 200
        assert client.get("/api/auth/status").json()["authenticated"] is True
        assert client.get("/api/bandi").status_code == 200
        preview = client.post("/api/pica/preview",
                              headers={"X-RankUp-Request": "1"},
                              files={"file": ("demo.xlsx", pica_fixture(),
                                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert preview.status_code == 200, preview.text
        token = preview.json()["token"]
        confirm = client.post("/api/pica/commit", headers={"X-RankUp-Request": "1"}, json={"token": token})
        assert confirm.status_code == 200, confirm.text
        assert client.get("/api/bandi").json()["bandi"][0]["codice"] == "26-02"
        assert len(client.get("/api/bandi/26-02/incarichi").json()["incarichi"]) == 2
        assert len(client.get("/api/bandi/26-02/candidature").json()["candidature"]) == 3
        up = client.post("/api/bandi/26-02/commissioni/import",
                         headers={"X-RankUp-Request": "1"},
                         files={"file": ("demo.xlsx", commissioni_fixture(),
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert up.status_code == 200, up.text
        assert len(client.get("/api/bandi/26-02/commissioni").json()["commissioni"]) == 2
        assignments=client.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        assert sorted(x["ssd"] for x in assignments)==["SSD-A","SSD-B"]
        unclassified=client.get("/api/bandi/26-02/schede/zip")
        assert unclassified.status_code == 400
        for sector,typ in [("SSD-A","ordinario"),("SSD-B","professionalizzante")]:
            res=client.put("/api/ssd/"+sector+"/classificazione",
                headers={"X-RankUp-Request":"1"},
                json={"tipo":typ,"riferimento":"Fonte documentale di prova"})
            assert res.status_code == 200, res.text
        exported=client.get("/api/bandi/26-02/schede/zip")
        assert exported.status_code == 200, exported.text
        with zipfile.ZipFile(BytesIO(exported.content)) as archive:
            pdfs=archive.namelist()
            assert len(pdfs)==3
            types=[]
            for name in pdfs:
                pdf=PdfReader(BytesIO(archive.read(name)))
                fields=pdf.get_fields()
                assert fields["ID-DOMANDA"].value in ("1001","1002")
                assert fields["P01"].value == "0"
                assert fields["TOT"].value == "0"
                assert fields["RANKUP-ID"].value.startswith("RU-26-02-")
                types.append("P19" in fields)
            assert types.count(True)==1
            assert types.count(False)==2
        repeat = client.post("/api/pica/preview",
                             headers={"X-RankUp-Request": "1"},
                             files={"file": ("demo.xlsx", pica_fixture(),
                                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert repeat.status_code == 409

        logout = client.post("/api/auth/logout", headers={"X-RankUp-Request": "1"})
        assert logout.status_code == 200
        assert client.get("/api/bandi").status_code == 401

    # Simula il vecchio bug: un SSD era vuoto e un altro copiato
    # impropriamente dall'autodichiarazione del candidato.
    with server.connect() as db:
        db.execute("DELETE FROM ssd_import_migrations")
        db.execute("UPDATE incarichi SET ssd='SSD-PERSONALE' WHERE ssd='SSD-A'")
        db.execute("UPDATE incarichi SET ssd='' WHERE ssd='SSD-B'")
    importlib.reload(server)
    with server.connect() as db:
        sectors=[r["ssd"] for r in db.execute("SELECT DISTINCT ssd FROM incarichi ORDER BY ssd")]
        repaired=db.execute("SELECT incarichi_corretti FROM ssd_import_migrations").fetchone()
        changes=db.execute("SELECT COUNT(*) AS n FROM ssd_import_rettifiche").fetchone()
    assert sectors == ["SSD-A","SSD-B"]
    assert repaired["incarichi_corretti"] == 2
    assert changes["n"] == 2
