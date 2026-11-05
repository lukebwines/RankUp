"""Gestione commissioni e riferimenti con dati esclusivamente sintetici."""
from io import BytesIO
import importlib
import json
from openpyxl import load_workbook
from fastapi.testclient import TestClient

from test_import import pica_fixture, commissioni_fixture
from commissioni import HEADERS, parse_commissioni


def _auth(client):
    result = client.post("/api/auth/login",
        headers={"X-RankUp-Request": "1"},
        json={"username":"test-admin","password":"PasswordFittiziaLunga12345!"})
    assert result.status_code == 200, result.text


def _up(client, path, method="POST", **kwargs):
    return client.request(method, path, headers={"X-RankUp-Request":"1"}, **kwargs)


def _commit_pica(client):
    preview = _up(client, "/api/pica/preview", files={
        "file": ("candidature.xlsx", pica_fixture(),
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert preview.status_code == 200, preview.text
    committed = _up(client, "/api/pica/commit",
        json={"token": preview.json()["token"]})
    assert committed.status_code == 200, committed.text


def test_manual_add_edit_import_template_and_notice_references(tmp_path, monkeypatch):
    monkeypatch.setenv("RANKUP_DATA_DIR", str(tmp_path))
    (tmp_path / "options.json").write_text(
        json.dumps({"admin_username": "test-admin",
                    "admin_password": "PasswordFittiziaLunga12345!"}))
    import server
    importlib.reload(server)

    with TestClient(server.app) as client:
        _auth(client)
        _commit_pica(client)
        code = "26-02"
        path = f"/api/bandi/{code}/commissioni"

        report = client.get(path + "/associazioni").json()
        assert report["mancanti"] == 1
        assert report["associate"] == 0

        # Il format e' il medesimo .xlsx che RankUp e' in grado di importare.
        for kind in ("vuoto", "cds_sedi", "esistenti"):
            response = client.get(path + "/modello?tipo=" + kind)
            assert response.status_code == 200
            assert response.content.startswith(b"PK")
            wb = load_workbook(BytesIO(response.content))
            ws = wb["Commissioni"]
            assert tuple(c.value for c in ws[1]) == HEADERS
            assert ws["E2"].value == "Bando 26-02"
            if kind == "cds_sedi":
                assert "ABILITANTE" in ws["A2"].value
                assert ws["B2"].value == "SEDE A"
                assert ws["C2"].value is None
            wb.close()

        # File con CdS/sede precompilati senza presidente: riga in sospeso.
        initial_template = client.get(path + "/modello?tipo=cds_sedi").content
        wb = load_workbook(BytesIO(initial_template))
        ws = wb["Commissioni"]
        ws["C2"] = "Presidente sintetico"
        ws["D2"] = "Componenti sintetici"
        ws["G2"] = "ANNO DI PROVA"
        buf = BytesIO()
        wb.save(buf)
        wb.close()
        parsed = parse_commissioni("file.xlsx", buf.getvalue(), code)
        assert len(parsed["rows"]) == 1
        assert parsed["numero_sedi"] == 1

        payload = {
            "cds":"CDS DEMO (ABILITANTE ALLA PROFESSIONE SANITARIA DI PROVA)",
            "sede":"SEDE A","presidente":"Presidente sintetico",
            "componenti":"Componenti sintetici","requisiti":"",
            "anno":"ANNO DI PROVA",
        }
        created = _up(client, path + "/manuale", json=payload)
        assert created.status_code == 200, created.text
        assert client.get(path + "/associazioni").json()["associate"] == 1
        assert _up(client, path + "/manuale", json=payload).status_code == 409

        rows = client.get(path).json()["commissioni"]
        assert len(rows) == 1
        assert rows[0]["id"] == created.json()["id"]
        assert rows[0]["requisiti"] == ""
        assert rows[0]["updated_at"]

        changed = dict(payload, presidente="Presidente revisionato",
                       expected_updated_at=rows[0]["updated_at"])
        edited = _up(client, path + "/" + str(rows[0]["id"]), method="PUT",
                     json=changed)
        assert edited.status_code == 200, edited.text
        assert edited.json()["modificata"]
        assert _up(client, path + "/" + str(rows[0]["id"]), method="PUT",
                   json=changed).status_code == 409

        # Importazione solo nuove: la commissione esistente resta invariata.
        supplied = commissioni_fixture()
        response = _up(client, path + "/import", files={
            "file": ("Commissioni.xlsx", supplied,
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        }, data={"mode":"solo_nuove"})
        assert response.status_code == 200, response.text
        assert response.json()["aggiunte"] == 1
        assert response.json()["aggiornate"] == 0
        assert response.json()["gia_presenti_o_inalterate"] == 1
        current = client.get(path).json()["commissioni"]
        assert len(current) == 2
        assert next(x for x in current if x["sede"]=="SEDE A")["presidente"] == "Presidente revisionato"

        # Aggiornamento massivo esplicito. Non elimina informazioni con celle vuote.
        wb = load_workbook(BytesIO(supplied))
        ws=wb.active
        ws["C2"] = "Presidente da aggiornamento Excel"
        ws["D2"] = ""  # Non cancella la composizione gia' acquisita.
        buf=BytesIO()
        wb.save(buf)
        wb.close()
        imported = _up(client, path + "/import", files={
            "file": ("Commissioni_aggiornate.xlsx", buf.getvalue(),
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        }, data={"mode":"aggiorna"})
        assert imported.status_code == 200, imported.text
        assert imported.json()["aggiornate"] >= 1
        latest = next(x for x in client.get(path).json()["commissioni"]
                      if x["sede"] == "SEDE A")
        assert latest["presidente"] == "Presidente da aggiornamento Excel"
        assert latest["componenti"] == "Componenti sintetici"

        # Bando: repertorio, protocollo e data modificabili, senza defaults.
        refs = client.get(f"/api/bandi/{code}/riferimenti")
        assert refs.status_code == 200
        assert not refs.json()["riferimenti"]["repertorio"]
        refs_payload = {
            "repertorio":"REF_TEST", "protocollo":"PROT_TEST",
            "data_avviso":"01/01/2026", "anno_accademico":"2026-2027",
            "expected_updated_at":None,
        }
        saved = _up(client, f"/api/bandi/{code}/riferimenti", method="PUT",
                    json=refs_payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["modificato"]
        assert _up(client, f"/api/bandi/{code}/riferimenti", method="PUT",
                   json=refs_payload).status_code == 409
        verified = client.get(f"/api/bandi/{code}/riferimenti").json()["riferimenti"]
        assert verified["repertorio"] == "REF_TEST"
        assert verified["protocollo"] == "PROT_TEST"

        # Nuova scheda prodotta con i dati del bando, non quelli dell'esempio.
        response = client.get(f"/api/bandi/{code}/schede/zip")
        assert response.status_code == 200, response.text
        import zipfile
        from pypdf import PdfReader
        with zipfile.ZipFile(BytesIO(response.content)) as package:
            assert len(package.namelist()) == 3
            pdf = PdfReader(BytesIO(package.read(package.namelist()[0])))
            field=pdf.get_fields()
            assert "REF_TEST" in field["REQUISITI"].value
            assert "PROT_TEST" in field["REQUISITI"].value
            assert "01/01/2026" in field["REQUISITI"].value
            assert "2026-2027" in pdf.pages[0].extract_text()

        with server.connect() as db:
            changes=db.execute("SELECT count(*) FROM commissioni_eventi").fetchone()[0]
            refs_events=db.execute("SELECT count(*) FROM bandi_riferimenti_eventi").fetchone()[0]
            assert changes >= 3
            assert refs_events == 1


def test_commission_template_skips_untouched_course_placeholders():
    # Un format con CdS/sede vuoti di presidente non e' una nomina.
    from commissioni import make_commission_template
    data = make_commission_template("26-02", [
        {"cds":"Corso sintetico","sede":"SEDE A"},
        {"cds":"Corso sintetico 2","sede":"SEDE B",
         "presidente":"Presidente sintetico"},
    ])
    parsed = parse_commissioni("format.xlsx", data, "26-02")
    assert parsed["numero_sedi"] == 1
    assert parsed["righe_da_compilare_omesse"] == 1
