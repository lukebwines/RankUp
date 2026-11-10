"""Azzeramento completo e controllato di dati fittizi, senza azioni sull'istanza reale."""
from io import BytesIO
import zipfile
from fastapi.testclient import TestClient
from test_pdf_import import _setup, _post, _edit, _preview, _confirm
from pypdf import PdfReader


def test_admin_reset_requires_password_and_preserves_backup_and_accounts(tmp_path,monkeypatch):
    server,admin,sheets=_setup(tmp_path,monkeypatch)
    with admin:
        created=_post(admin,"/api/utenti",
                      json={"username":"operatore_prova","password":"PasswordProva123"})
        assert created.status_code==200,created.text
        pdf=next(iter(sheets.values()))
        altered=_edit(pdf,{"P01":"2","TOT":"2",
                           "STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        preview=_preview(admin,[("scheda.pdf",altered)])
        assert _confirm(admin,preview["token"])["acquisite"]==1

        # Configurazioni permanenti verificate prima dell'azzeramento.
        with server.connect() as db:
            ssd_before=db.execute(
                "SELECT ssd,tipo FROM ssd_classificazioni ORDER BY ssd").fetchall()
            commissions_before=db.execute(
                "SELECT impronta FROM commissioni_catalogo ORDER BY impronta").fetchall()
            assert len(ssd_before)>=1
            assert len(commissions_before)>=1
            assert db.execute("SELECT COUNT(*) FROM commissioni").fetchone()[0]>=1

        status=admin.get("/api/admin/azzeramento")
        assert status.status_code==200,status.text
        summary=status.json()
        assert summary["bandi"]==1
        assert summary["candidature"]==3
        assert summary["valutazioni"]==1
        assert summary["originali"]>=2
        assert summary["backup_obbligatorio"] is True

        payload={"conferma":"AZZERA TUTTI I DATI RANKUP",
                 "password_admin":"PasswordFittiziaLunga12345!"}
        assert _post(admin,"/api/admin/azzeramento",
                     json={**payload,"password_admin":"sbagliata"}).status_code==403
        assert _post(admin,"/api/admin/azzeramento",
                     json={**payload,"conferma":"AZZERA"}).status_code==400
        assert admin.get("/api/bandi").json()["bandi"]

        with TestClient(server.app) as user:
            login=_post(user,"/api/auth/login",
                        json={"username":"operatore_prova","password":"PasswordProva123"})
            assert login.status_code==200
            assert user.get("/api/admin/azzeramento").status_code==403
            assert _post(user,"/api/admin/azzeramento",json=payload).status_code==403

        response=_post(admin,"/api/admin/azzeramento",json=payload)
        assert response.status_code==200,response.text
        name=response.json()["backup"]
        assert name.endswith(".zip")
        backup=admin.get("/api/admin/azzeramento/backup/"+name)
        assert backup.status_code==200,backup.text[:300] if backup.status_code!=200 else ""
        with zipfile.ZipFile(BytesIO(backup.content)) as z:
            assert z.testzip() is None
            assert "rankup.sqlite3" in z.namelist()
            assert "manifest.json" in z.namelist()
            assert any(f.startswith("sources/") for f in z.namelist())
            assert any(f.endswith(".pdf") for f in z.namelist())
            assert "rankup_utenti" in z.read("rankup.sqlite3").decode("latin1")

        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM bandi").fetchone()[0]==0
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf").fetchone()[0]==0
            assert db.execute("SELECT COUNT(*) FROM rankup_utenti").fetchone()[0]==1
            assert db.execute("SELECT COUNT(*) FROM commissioni").fetchone()[0]==0
            assert db.execute("SELECT ssd,tipo FROM ssd_classificazioni ORDER BY ssd").fetchall()==ssd_before
            assert db.execute("SELECT impronta FROM commissioni_catalogo ORDER BY impronta").fetchall()==commissions_before
            assert db.execute("SELECT COUNT(*) FROM pica_tipo_cds_dizionario").fetchone()[0]==5
        assert not list((tmp_path/"sources").rglob("*"))
        assert not list((tmp_path/"pending").rglob("*"))
        assert admin.get("/api/bandi").json()["bandi"]==[]

        # Le credenziali dell'operatore sopravvivono al reset.
        with TestClient(server.app) as user:
            login=_post(user,"/api/auth/login",
                        json={"username":"operatore_prova","password":"PasswordProva123"})
            assert login.status_code==200
            assert user.get("/api/admin/azzeramento").status_code==403
            assert user.get("/api/admin/azzeramento/backup/"+name).status_code==403

        # Una seconda esecuzione non cancella il backup precedente.
        second=_post(admin,"/api/admin/azzeramento",json=payload)
        assert second.status_code==200,second.text
        assert admin.get("/api/admin/azzeramento/backup/"+name).status_code==200
