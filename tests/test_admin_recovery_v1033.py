"""Test sintetici del recupero Admin: nessun segreto reale, nessun DB produzione."""
import importlib
import json
import re
import time

from fastapi.testclient import TestClient
from db_lifecycle import SCHEMA_VERSION
from test_pdf_import import _setup, _post

OLD="PasswordFittiziaLunga12345!"
NEW="UnaPasswordAdminNuova98765!"
AFTER="UnaPasswordAdminDopoRecupero123!"


def _new_admin(server):
    c=TestClient(server.app)
    r=_post(c,"/api/auth/login",json={"username":"test-admin","password":NEW})
    return c,r


def _issue_from_home_assistant(tmp_path,monkeypatch,capsys,request_id):
    options={"admin_username":"test-admin","admin_password":OLD,
             "admin_recovery_request":request_id}
    (tmp_path/"options.json").write_text(json.dumps(options))
    monkeypatch.setenv("RANKUP_DATA_DIR",str(tmp_path))
    import server
    importlib.reload(server)
    output=capsys.readouterr().out
    found=re.search(r"codice monouso:\s+([A-Za-z0-9_-]+)",output)
    assert found is not None, "Il codice non viene mostrato nel log HA dopo la nuova richiesta"
    return server,found.group(1)


def test_legacy_admin_migrates_to_salted_hash_without_data_loss(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        with server.connect() as db:
            row=db.execute("""SELECT username,password_salt,password_hash
                FROM rankup_admin_auth WHERE id=1""").fetchone()
            assert row["username"]=="test-admin"
            assert len(row["password_salt"])>=32
            assert len(row["password_hash"])>=64
            assert row["password_hash"]!=OLD
            assert db.execute("PRAGMA user_version").fetchone()[0]==SCHEMA_VERSION
        assert OLD.encode() not in (tmp_path/"rankup.sqlite3").read_bytes()
        assert admin.get("/api/bandi").status_code==200


def test_admin_change_revokes_sessions_and_legacy_config_cannot_reset(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        wrong=_post(admin,"/api/auth/admin/change-password",json={
            "password_attuale":"PasswordErrata123!",
            "nuova_password":NEW})
        assert wrong.status_code==403
        changed=_post(admin,"/api/auth/admin/change-password",json={
            "password_attuale":OLD,"nuova_password":NEW})
        assert changed.status_code==200,changed.text
        assert admin.get("/api/bandi").status_code==401
        assert _post(admin,"/api/auth/login",json={
            "username":"test-admin","password":OLD}).status_code==401
        renewed=_post(admin,"/api/auth/login",json={
            "username":"test-admin","password":NEW})
        assert renewed.status_code==200,renewed.text
        import server as module
        importlib.reload(module)  # Password HA legacy rimane ma non viene riattivata.
        with TestClient(module.app) as after_restart:
            assert _post(after_restart,"/api/auth/login",json={
                "username":"test-admin","password":OLD}).status_code==401
            assert _post(after_restart,"/api/auth/login",json={
                "username":"test-admin","password":NEW}).status_code==200
            audit=after_restart.get("/api/admin/sicurezza/eventi?ambito=sicurezza")
            assert audit.status_code==200
            assert "password_admin" in audit.text


def test_admin_recovery_requires_ha_request_is_once_only_and_survives_restart(
        tmp_path,monkeypatch,capsys):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        old_bandi=len(admin.get("/api/bandi").json()["bandi"])
    module,code=_issue_from_home_assistant(tmp_path,monkeypatch,capsys,1)
    assert len(code)>=40
    with TestClient(module.app) as guest:
        denied=_post(guest,"/api/auth/admin/recover",json={
            "codice":"x"*43,"nuova_password":AFTER})
        assert denied.status_code==403
        recovered=_post(guest,"/api/auth/admin/recover",json={
            "codice":code,"nuova_password":AFTER})
        assert recovered.status_code==200,recovered.text
        replay=_post(guest,"/api/auth/admin/recover",json={
            "codice":code,"nuova_password":"PasswordDiversaTest9876!"})
        assert replay.status_code==403
        assert _post(guest,"/api/auth/login",json={
            "username":"test-admin","password":OLD}).status_code==401
        assert _post(guest,"/api/auth/login",json={
            "username":"test-admin","password":AFTER}).status_code==200
        assert len(guest.get("/api/bandi").json()["bandi"])==old_bandi
    importlib.reload(module)
    assert "codice monouso" not in capsys.readouterr().out
    with TestClient(module.app) as renewed:
        assert _post(renewed,"/api/auth/login",json={
            "username":"test-admin","password":AFTER}).status_code==200


def test_recovery_expires_is_rate_limited_and_ha_can_request_new_code(
        tmp_path,monkeypatch,capsys):
    server,admin,_=_setup(tmp_path,monkeypatch)
    module,code=_issue_from_home_assistant(tmp_path,monkeypatch,capsys,1)
    with module.connect() as db:
        db.execute("UPDATE rankup_admin_auth SET recovery_scade=? WHERE id=1",
                   (time.time()-1,))
    with TestClient(module.app) as guest:
        assert _post(guest,"/api/auth/admin/recover",json={
            "codice":code,"nuova_password":NEW}).status_code==403
    module,code=_issue_from_home_assistant(tmp_path,monkeypatch,capsys,2)
    with TestClient(module.app) as guest:
        for i in range(5):
            assert _post(guest,"/api/auth/admin/recover",json={
                "codice":str(i)*43,"nuova_password":NEW}).status_code==403
        with module.connect() as db:
            row=db.execute("""SELECT recovery_sha256,recovery_tentativi
                              FROM rankup_admin_auth WHERE id=1""").fetchone()
            assert row["recovery_sha256"]==""
            assert row["recovery_tentativi"]>=5
        assert _post(guest,"/api/auth/admin/recover",json={
            "codice":code,"nuova_password":NEW}).status_code==403


def test_recovery_not_available_to_operator_as_admin_change(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        created=_post(admin,"/api/utenti",json={
            "username":"utente-test","password":"TemporaneaDiProva123!"})
        assert created.status_code==200
    with TestClient(server.app) as operator:
        assert _post(operator,"/api/auth/login",json={
            "username":"utente-test","password":"TemporaneaDiProva123!"}).status_code==200
        assert _post(operator,"/api/auth/admin/change-password",json={
            "password_attuale":"TemporaneaDiProva123!","nuova_password":NEW}).status_code==403
        assert operator.get("/api/utenti").status_code==403


def test_ui_marks_admin_recovery_and_hash_without_exposing_old_option():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]/"rankup"
    index=(root/"app/static/index.html").read_text(encoding="utf-8")
    config=(root/"config.yaml").read_text(encoding="utf-8")
    assert 'id="recovery-open"' in index
    assert "auth/admin/recover" in index
    assert "auth/admin/change-password" in index
    assert "admin_recovery_request: int(0,999999999)" in config
    assert "admin_password: password?" in config



def test_unconfigured_fresh_install_recovers_without_plaintext_option(
        tmp_path,monkeypatch,capsys):
    monkeypatch.setenv("RANKUP_DATA_DIR",str(tmp_path))
    (tmp_path/"options.json").write_text(json.dumps({
        "admin_username":"test-admin","admin_recovery_request":1}))
    import server
    importlib.reload(server)
    logged=capsys.readouterr().out
    match=re.search(r"codice monouso:\s+([A-Za-z0-9_-]+)",logged)
    assert match is not None
    with TestClient(server.app) as guest:
        status=guest.get("/api/auth/status")
        assert status.json()["configured"] is False
        assert _post(guest,"/api/auth/login",json={
            "username":"test-admin","password":NEW}).status_code==503
        issued=_post(guest,"/api/auth/admin/recover",json={
            "codice":match.group(1),"nuova_password":NEW})
        assert issued.status_code==200,issued.text
        assert _post(guest,"/api/auth/login",json={
            "username":"test-admin","password":NEW}).status_code==200
        assert guest.get("/api/utenti").status_code==200


def test_ha_recovery_revokes_live_admin_session(tmp_path,monkeypatch,capsys):
    server,admin,_=_setup(tmp_path,monkeypatch)
    module,code=_issue_from_home_assistant(tmp_path,monkeypatch,capsys,1)
    with TestClient(module.app) as existing,TestClient(module.app) as guest:
        assert _post(existing,"/api/auth/login",json={
            "username":"test-admin","password":OLD}).status_code==200
        assert existing.get("/api/bandi").status_code==200
        done=_post(guest,"/api/auth/admin/recover",json={
            "codice":code,"nuova_password":NEW})
        assert done.status_code==200
        assert existing.get("/api/bandi").status_code==401
        assert _post(existing,"/api/auth/login",json={
            "username":"test-admin","password":OLD}).status_code==401
        assert _post(existing,"/api/auth/login",json={
            "username":"test-admin","password":NEW}).status_code==200
