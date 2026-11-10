"""Regressioni dei permessi sull'archivio ZIP finale (dati sintetici)."""
from pathlib import Path
from zipfile import ZipFile
from io import BytesIO

from fastapi.testclient import TestClient

from test_pdf_import import _setup, _post
from test_graduatorie import _complete_all


def test_operatore_puo_usare_zip_schede_ma_non_archivio_finale(tmp_path, monkeypatch):
    server, admin, sheets = _setup(tmp_path, monkeypatch)
    base = "/api/bandi/26-02"
    with admin:
        created = _post(admin, "/api/utenti", json={
            "username": "operatore_test", "password": "PasswordTest123!"
        })
        assert created.status_code == 200, created.text

        with TestClient(server.app) as operatore:
            login = _post(operatore, "/api/auth/login", json={
                "username": "operatore_test", "password": "PasswordTest123!"
            })
            assert login.status_code == 200, login.text
            assert login.json()["role"] == "user"

            # Lo ZIP operativo delle schede resta disponibile.
            working_zip = operatore.get(base + "/schede/zip")
            assert working_zip.status_code == 200, working_zip.text
            with ZipFile(BytesIO(working_zip.content)) as archive:
                assert archive.namelist()

            # Nessuna API di archivio finale deve essere raggiungibile
            # da un operatore, anche chiamandola fuori dall'interfaccia.
            assert operatore.get("/api/archivio").status_code == 403
            assert operatore.get(base + "/archivio/verifica").status_code == 403
            assert _post(operatore, base + "/archivio/chiudi", json={}).status_code == 403
            assert _post(operatore, base + "/archivio/chiudi/avvia", json={}).status_code == 403
            assert operatore.get(base + "/archivio/chiudi/lavoro-attivo").status_code == 403
            assert operatore.get(base + "/archivio/chiudi/lavoro/inesistente").status_code == 403
            assert _post(operatore, "/api/archivio/ricerca-candidati", json={
                "testo": "TEST", "criterio": "cognome"
            }).status_code == 403
            with server.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM bandi_archivi").fetchone()[0] == 0

            # Solo l'Admin può completare la chiusura e generare lo ZIP finale.
            _complete_all(admin, sheets)
            group = admin.get(base + "/verbali/graduatorie").json()["graduatorie"][0]
            final = _post(admin, base + "/verbali/graduatorie/genera-definitivo",
                          json={"cds": group["cds"], "sede": group["sede"]})
            assert final.status_code == 200, final.text
            gid = admin.get(base + "/verbali/graduatorie").json()["graduatorie"][0]["conferma"]["id"]
            approval = _post(admin, base + "/graduatorie/approvazioni",
                             json={"graduatorie_ids": [gid]})
            assert approval.status_code == 200, approval.text
            confirmed = _post(admin, base +
                              f"/graduatorie/approvazioni/{approval.json()['id']}/conferma",
                              json={"estremi_atto": "RIFERIMENTO SINTETICO DI COLLAUDO"})
            assert confirmed.status_code == 200, confirmed.text
            closed = _post(admin, base + "/archivio/chiudi", json={})
            assert closed.status_code == 200, closed.text
            archive_id = closed.json()["archivio_id"]

            assert admin.get(f"/api/archivio/{archive_id}/zip").status_code == 200
            assert operatore.get(f"/api/archivio/{archive_id}/zip").status_code == 403
            reopening = _post(operatore, base + "/archivio/riapri", json={
                "motivazione": "Tentativo sintetico non autorizzato",
                "riferimento": "ATTO SINTETICO DI COLLAUDO"
            })
            assert reopening.status_code == 403, reopening.text
            assert admin.get(base).json()["bando"]["stato"] == "Archiviato"


def test_menu_archivio_finale_riservato_a_admin():
    root = Path(__file__).resolve().parents[1]
    html = (root / "rankup/app/static/index.html").read_text(encoding="utf-8")
    assert 'data-page="archivio" id="admin-archive-link" style="display:none"' in html
    assert '$("admin-archive-link").style.display="none";' in html
    assert '$("admin-archive-link").style.display=state.role==="admin"?"flex":"none";' in html
    assert '"azzeramento","archivio"].includes(state.page)' in html
    assert 'if(page==="archivio"&&state.role!=="admin")return;' in html
