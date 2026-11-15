"""RankUP 1.0.32: registri Admin e credenziali operatori (dati esclusivamente sintetici)."""
from fastapi.testclient import TestClient
from test_pdf_import import _setup,_post


def _operator(server,username,password):
    user=TestClient(server.app)
    answer=_post(user,"/api/auth/login",json={"username":username,"password":password})
    assert answer.status_code==200,answer.text
    return user,answer.json()


def test_login_events_and_audit_admin_only(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        for _ in range(2):
            fail=_post(admin,"/api/auth/login",json={
                "username":"test-admin","password":"CredenzialeErrataDiTest"})
            assert fail.status_code==401
        valid=_post(admin,"/api/auth/login",json={
            "username":"test-admin","password":"PasswordFittiziaLunga12345!"})
        assert valid.status_code==200
        report=admin.get("/api/admin/sicurezza/eventi?ambito=sicurezza")
        assert report.status_code==200,report.text
        rows=report.json()["eventi"]
        assert sum(r["evento"]=="login_fallito" for r in rows)>=2
        assert any(r["evento"]=="login_ok" for r in rows)
        assert all("password" not in r["percorso"].lower() or
                   r["percorso"]=="/api/auth/change-password" for r in rows)
        assert "CredenzialeErrataDiTest" not in report.text


def test_first_login_password_change_and_session_revocation(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        added=_post(admin,"/api/utenti",json={
            "username":"operatore-prova","password":"PasswordInizialeDiTest123!"})
        assert added.status_code==200,added.text
        user,login=_operator(server,"operatore-prova","PasswordInizialeDiTest123!")
        with user:
            assert login["role"]=="user"
            assert login["password_change_required"] is True
            assert user.get("/api/bandi").status_code==403
            assert user.get("/api/admin/sicurezza/eventi").status_code==403
            assert user.get("/api/admin/registro-attivita").status_code==403
            wrong=_post(user,"/api/auth/change-password",json={
                "password_attuale":"PasswordSbagliata","nuova_password":"NuovaPasswordOperatore789!"})
            assert wrong.status_code==403
            changed=_post(user,"/api/auth/change-password",json={
                "password_attuale":"PasswordInizialeDiTest123!",
                "nuova_password":"NuovaPasswordOperatore789!"})
            assert changed.status_code==200,changed.text
            assert user.get("/api/bandi").status_code==401
        user2,login2=_operator(server,"operatore-prova","NuovaPasswordOperatore789!")
        with user2:
            assert login2["password_change_required"] is False
            assert user2.get("/api/bandi").status_code==200
            assert user2.get("/api/admin/diagnostica").status_code==403
            assert user2.get("/api/admin/sicurezza/politica").status_code==403
            assert user2.get("/api/admin/sicurezza/eventi").status_code==403
            again=_post(user2,"/api/auth/change-password",json={
                "password_attuale":"NuovaPasswordOperatore789!",
                "nuova_password":"PasswordPersonaleDiversa789!"})
            assert again.status_code==200
        events=admin.get("/api/admin/sicurezza/eventi?ambito=sicurezza").json()["eventi"]
        assert sum(r["evento"]=="password_personale" for r in events)>=2
        with server.connect() as db:
            saved=db.execute("""SELECT password_modificata_il,password_cambio_obbligatorio
                 FROM rankup_utenti WHERE username='operatore-prova'""").fetchone()
            assert saved["password_modificata_il"]
            assert saved["password_cambio_obbligatorio"]==0


def test_operation_audit_is_paged_and_preserved(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        created=_post(admin,"/api/bandi",json={"codice":"PROVA-AUDIT",
                                                  "anno_accademico":"2027-2028"})
        assert created.status_code==200,created.text
        report=admin.get("/api/admin/sicurezza/eventi?ambito=operazioni&pagina=1&limite=1")
        assert report.status_code==200,report.text
        assert report.json()["totale"]>0
        assert len(report.json()["eventi"])==1
        full=admin.get("/api/admin/sicurezza/eventi?ambito=operazioni&limite=100")
        assert any(r["oggetto"]=="PROVA-AUDIT" and
                   r["percorso"]=="/api/bandi" for r in full.json()["eventi"]) is False
        assert any(r["evento"]=="http_modifica" for r in full.json()["eventi"])
        assert admin.get("/api/admin/sicurezza/eventi?limite=10000").status_code==400
        with server.audit_connect() as db:
            assert db.execute("SELECT COUNT(*) FROM rankup_audit_eventi").fetchone()[0]>0


def test_optional_180_day_policy_and_legacy_unknown_date(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        assert admin.get("/api/admin/sicurezza/politica").json()["password_max_giorni"]==0
        added=_post(admin,"/api/utenti",json={
            "username":"operatore-scadenza","password":"PasswordInizialeDiTest123!"})
        assert added.status_code==200
        policy=admin.put("/api/admin/sicurezza/politica",
            json={"password_max_giorni":180},headers={"X-RankUp-Request":"1"})
        assert policy.status_code==200,policy.text
        with server.connect() as db:
            db.execute("""UPDATE rankup_utenti SET password_cambio_obbligatorio=0,
                password_modificata_il=NULL WHERE username='operatore-scadenza'""")
        user,login=_operator(server,"operatore-scadenza","PasswordInizialeDiTest123!")
        with user:
            assert login["password_change_required"] is True
            assert user.get("/api/bandi").status_code==403
        restored=admin.put("/api/admin/sicurezza/politica",
            json={"password_max_giorni":0},headers={"X-RankUp-Request":"1"})
        assert restored.status_code==200,restored.text
