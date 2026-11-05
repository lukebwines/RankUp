from io import BytesIO
from urllib.parse import quote
import json
import zipfile
from pypdf import PdfReader
from openpyxl import Workbook, load_workbook
from incarichi_fields import EXTRA_DB_FIELDS, normalize_excel_date
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
    columns = [
        "ID domanda", "Stato", "Corso di Studio", "sede",
        "Nome insegnamento padre", "Descrizione Copertura",
        "ID Copertura", "Cognome", "Nome", "Codice fiscale",
        "ssd", "Settore scientifico disciplinare",
    ]
    extra_columns = [
        "Anno del Corso", "Tipo Corso", "Tipo CDS", "Ciclo",
        "INIZIO", "FINE", "Codice Partizione Studenti",
        "Partizione Studenti", "Peso Copertura", "Ore", "Stato coperture",
    ]
    extra_a = [
        3, "L", "contratto esterno oneroso CSA", "annuale",
        46394, 46660, "ALTR", "Matricole DISPARI",
        2, None, "Nessuna assegnata",
    ]
    extra_b = [
        2, "L", "contratto esterno oneroso CSA", "primo semestre",
        46394, 46485, None, None,
        3, None, "Nessuna assegnata",
    ]
    return make_excel([
        ["univr 26-02 - Esportazione dati"],
        columns + extra_columns,
        [1001, "presentata", "CDS DEMO", "SEDE A",
         "INSEGNAMENTO X", "MODULO X", 100, "Candidato", "Uno",
         "CODICEFISCALE", "SSD-A", ""] + extra_a,
        [1001, "presentata", "CDS DEMO", "SEDE A",
         "INSEGNAMENTO Y", "MODULO Y", 101, "Candidato", "Uno",
         "CODICEFISCALE", "MEDS-24/C", ""] + extra_b,
        [1002, "presentata", "CDS DEMO", "SEDE A",
         "INSEGNAMENTO X", "MODULO X", 100, "Candidato", "Due",
         "CODICEFISCALE2", "SSD-A", "SSD-PERSONALE"] + extra_a,
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
    assert result["rows"][0]["incarico"]["anno_corso"] == "3"
    assert result["rows"][0]["incarico"]["tipo_cds"] == "contratto esterno oneroso CSA"
    assert result["rows"][0]["incarico"]["inizio"] == "46394"
    assert result["rows"][0]["incarico"]["fine"] == "46660"
    assert result["rows"][0]["incarico"]["codice_partizione"] == "ALTR"
    assert result["rows"][0]["incarico"]["partizione_studenti"] == "Matricole DISPARI"
    assert result["rows"][0]["incarico"]["peso_copertura"] == "2"
    assert result["rows"][0]["incarico"]["ore"] == ""
    assert result["rows"][0]["incarico"]["stato_coperture"] == "Nessuna assegnata"
    assert [x["incarico"]["ssd"] for x in result["rows"]] == ["SSD-A", "MEDS-24/C", "SSD-A"]
    assert result["rows"][0]["id_pica"] == result["rows"][1]["id_pica"]
    assert result["rows"][0]["incarico"]["key"] != result["rows"][1]["incarico"]["key"]


def test_date_conversion_does_not_change_source():
    assert normalize_excel_date("46394") == "2027-01-07"
    assert normalize_excel_date("46660") == "2027-09-30"
    assert normalize_excel_date("") == ""
    assert normalize_excel_date("testo non valido") == ""


def test_conflicting_assignment_metadata_requires_verification():
    wb = load_workbook(BytesIO(pica_fixture()))
    ws = wb.active
    headers = [cell.value for cell in ws[2]]
    column = headers.index("Peso Copertura") + 1
    ws.cell(row=5, column=column).value = 99
    output = BytesIO()
    wb.save(output)
    with pytest.raises(ValueError, match="peso_copertura"):
        parse_pica("demo.xlsx", output.getvalue())


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
        assert sorted(x["ssd"] for x in assignments)==["MEDS-24/C","SSD-A"]
        enriched = [x for x in assignments if x["ssd"]=="SSD-A"][0]
        assert enriched["anno_corso"]=="3"
        assert enriched["ciclo"]=="annuale"
        assert enriched["inizio_pica"]=="46394"
        assert enriched["inizio_iso"]=="2027-01-07"
        assert enriched["fine_iso"]=="2027-09-30"
        assert enriched["codice_partizione"]=="ALTR"
        assert enriched["partizione_studenti"]=="Matricole DISPARI"
        assert enriched["peso_copertura"]=="2"
        assert enriched["ore"]==""  # No inference from coverage weight
        assert enriched["stato_coperture"]=="Nessuna assegnata"
        acquisition=client.get("/api/bandi/26-02/incarichi").json()["acquisizione_metadati"]
        assert acquisition["metodo"]=="importazione"
        assert acquisition["incarichi_verificati"]==2
        # I settori identificati sono ordinari per impostazione predefinita.
        defaults=client.get("/api/bandi/26-02/ssd").json()["ssd"]
        assert len(defaults)==2
        assert all(x["tipo"]=="ordinario" and x["tipo_salvato"] is None for x in defaults)
        pdf_default=client.get("/api/bandi/26-02/schede/zip")
        assert pdf_default.status_code==200, pdf_default.text
        with zipfile.ZipFile(BytesIO(pdf_default.content)) as archive:
            assert all("P19" not in PdfReader(BytesIO(archive.read(name))).get_fields()
                       for name in archive.namelist())

        # La selezione professionalizzante salva anche SSD con '/' e senza fonte.
        res=client.put("/api/ssd/"+quote("MEDS-24/C",safe="")+"/classificazione",
            headers={"X-RankUp-Request":"1"},
            json={"tipo":"professionalizzante"})
        assert res.status_code==200, res.text
        saved=client.get("/api/bandi/26-02/ssd").json()["ssd"]
        selected=next(x for x in saved if x["ssd"]=="MEDS-24/C")
        assert selected["tipo"]=="professionalizzante"
        assert selected["tipo_salvato"]=="professionalizzante"
        assert selected["riferimento"]==""
        other=next(x for x in saved if x["ssd"]=="SSD-A")
        assert other["tipo"]=="ordinario" and other["tipo_salvato"] is None
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
        # Un codice PICA 'NN' non viene classificato automaticamente.
        with server.connect() as db:
            db.execute("UPDATE incarichi SET ssd='NN' WHERE ssd='SSD-A'")
        unresolved=client.get("/api/bandi/26-02/ssd").json()["ssd"]
        assert any(x["ssd"]=="NN" and x["tipo"] is None for x in unresolved)
        assert client.get("/api/bandi/26-02/schede/zip").status_code==400
        with server.connect() as db:
            db.execute("UPDATE incarichi SET ssd='SSD-A' WHERE ssd='NN'")

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
        db.execute("UPDATE incarichi SET ssd='' WHERE ssd='MEDS-24/C'")
        db.execute("DELETE FROM incarichi_metadata_migrations")
        db.execute("UPDATE incarichi SET " + ", ".join(
            field+"=''" for field in EXTRA_DB_FIELDS
        ))
    importlib.reload(server)
    with server.connect() as db:
        sectors=[r["ssd"] for r in db.execute("SELECT DISTINCT ssd FROM incarichi ORDER BY ssd")]
        repaired=db.execute("SELECT incarichi_corretti FROM ssd_import_migrations").fetchone()
        changes=db.execute("SELECT COUNT(*) AS n FROM ssd_import_rettifiche").fetchone()
        metadata=db.execute("SELECT * FROM incarichi_metadata_migrations").fetchone()
        meta_audit=db.execute("SELECT COUNT(*) AS n FROM incarichi_metadata_audit").fetchone()
        refreshed=db.execute("SELECT ssd,anno_corso,inizio_pica,inizio_iso,ore FROM incarichi WHERE ssd='SSD-A'").fetchone()
    assert sectors == ["SSD-A","MEDS-24/C"]
    assert repaired["incarichi_corretti"] == 2
    assert changes["n"] == 2
    assert metadata["metodo"] == "migrazione"
    assert metadata["incarichi_verificati"] == 2
    assert metadata["incarichi_aggiornati"] == 2
    assert meta_audit["n"] == 2
    assert refreshed["anno_corso"] == "3"
    assert refreshed["inizio_pica"] == "46394"
    assert refreshed["inizio_iso"] == "2027-01-07"
    assert refreshed["ore"] == ""
