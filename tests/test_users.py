"""Test multiutenza RankUp, con account e procedure interamente sintetici."""
import importlib
import json
from pathlib import Path
from fastapi.testclient import TestClient
from test_pdf_import import _post, _setup


def _empty_setup(tmp_path,monkeypatch):
    monkeypatch.setenv("RANKUP_DATA_DIR",str(tmp_path))
    (tmp_path/"options.json").write_text(json.dumps({
        "admin_username":"admin_fittizio",
        "admin_password":"CredenzialeDiCollaudoLunga123!"
    }))
    import server
    importlib.reload(server)
    client=TestClient(server.app)
    admin=_post(client,"/api/auth/login",json={
        "username":"admin_fittizio",
        "password":"CredenzialeDiCollaudoLunga123!"
    })
    assert admin.status_code==200,admin.text
    assert admin.json()["role"]=="admin"
    return server,client


def test_admin_creates_operational_user_with_eight_character_password(tmp_path,monkeypatch):
    server,admin=_empty_setup(tmp_path,monkeypatch)
    with admin:
        short=_post(admin,"/api/utenti",json={
            "username":"operatore",
            "password":"Abc1234"
        })
        assert short.status_code==422,short.text
        reserved=_post(admin,"/api/utenti",json={
            "username":"ADMIN_FITTIZIO",
            "password":"Abc12345"
        })
        assert reserved.status_code==409,reserved.text
        created=_post(admin,"/api/utenti",json={
            "username":"operatore",
            "password":"Abc12345"
        })
        assert created.status_code==200,created.text
        uid=created.json()["utente"]["id"]
        assert created.json()["utente"]["username"]=="operatore"
        assert "password" not in created.json()
        duplicate=_post(admin,"/api/utenti",json={
            "username":"OPERATORE",
            "password":"AltraPass123"
        })
        assert duplicate.status_code==409,duplicate.text
        listed=admin.get("/api/utenti")
        assert listed.status_code==200,listed.text
        data=listed.json()
        assert data["password_minima"]==8
        assert len(data["utenti"])==1
        assert data["utenti"][0]["id"]==uid
        assert data["admin"]["username"]=="admin_fittizio"
        assert "password_hash" not in str(data)

        with server.connect() as db:
            row=db.execute("""
                SELECT username,password_salt,password_hash,attivo,credenziali_versione
                FROM rankup_utenti WHERE id=?
            """,(uid,)).fetchone()
            assert row["username"]=="operatore"
            assert row["attivo"]==1
            assert row["credenziali_versione"]==1
            assert row["password_hash"]!="Abc12345"
            assert len(row["password_salt"])>=32
            audit=db.execute("""
                SELECT azione,autore FROM rankup_utenti_eventi WHERE utente_id=?
            """,(uid,)).fetchone()
            assert audit["azione"]=="Creato account operativo"
            assert audit["autore"]=="admin_fittizio"
        with TestClient(server.app) as user:
            login=_post(user,"/api/auth/login",json={
                "username":"operatore","password":"Abc12345"
            })
            assert login.status_code==200,login.text
            assert login.json()["role"]=="user"
            assert login.json()["username"]=="operatore"
            status=user.get("/api/auth/status").json()
            assert status["authenticated"] is True
            assert status["role"]=="user"
            assert status["username"]=="operatore"
            assert user.get("/api/bandi").status_code==200
            assert user.get("/api/utenti").status_code==403
            assert _post(user,"/api/utenti",json={
                "username":"self_admin","password":"Another123"
            }).status_code==403
            assert user.put(f"/api/utenti/{uid}/stato",
                headers={"X-RankUp-Request":"1"},
                json={"attivo":False}).status_code==403
            assert user.put(f"/api/utenti/{uid}/password",
                headers={"X-RankUp-Request":"1"},
                json={"password":"Another123"}).status_code==403


def test_user_can_manage_bando_but_audit_uses_actual_identity(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        result=_post(admin,"/api/utenti",json={
            "username":"staff_test","password":"12345678"
        })
        assert result.status_code==200,result.text
        with TestClient(server.app) as user:
            auth=_post(user,"/api/auth/login",json={
                "username":"staff_test","password":"12345678"
            })
            assert auth.status_code==200,auth.text
            assert user.get("/api/bandi/26-02/commissioni").status_code==200
            assert user.get("/api/bandi/26-02/valutazioni").status_code==200
            changed=user.put("/api/bandi/26-02/riferimenti",
                headers={"X-RankUp-Request":"1"},
                json={
                    "repertorio":"RIFERIMENTO SINTETICO",
                    "protocollo":"",
                    "data_avviso":"",
                    "anno_accademico":"",
                    "expected_updated_at":None
                })
            assert changed.status_code==200,changed.text
            assert changed.json()["modificato"] is True
            with server.connect() as db:
                event=db.execute("""
                    SELECT autore FROM bandi_riferimenti_eventi
                    ORDER BY id DESC LIMIT 1
                """).fetchone()
                assert event["autore"]=="staff_test"
        assert admin.get("/api/auth/status").json()["role"]=="admin"


def test_disable_and_password_reset_revoke_existing_sessions(tmp_path,monkeypatch):
    server,admin=_empty_setup(tmp_path,monkeypatch)
    with admin:
        result=_post(admin,"/api/utenti",json={
            "username":"collaboratore","password":"abcdefgh"
        })
        assert result.status_code==200,result.text
        uid=result.json()["utente"]["id"]
        with TestClient(server.app) as user:
            login=_post(user,"/api/auth/login",json={
                "username":"collaboratore","password":"abcdefgh"
            })
            assert login.status_code==200,login.text
            disabled=admin.put(f"/api/utenti/{uid}/stato",
                headers={"X-RankUp-Request":"1"},
                json={"attivo":False})
            assert disabled.status_code==200,disabled.text
            assert user.get("/api/bandi").status_code==401
            assert _post(user,"/api/auth/login",json={
                "username":"collaboratore","password":"abcdefgh"
            }).status_code==401
            enabled=admin.put(f"/api/utenti/{uid}/stato",
                headers={"X-RankUp-Request":"1"},
                json={"attivo":True})
            assert enabled.status_code==200,enabled.text
            again=_post(user,"/api/auth/login",json={
                "username":"collaboratore","password":"abcdefgh"
            })
            assert again.status_code==200,again.text
            weak=admin.put(f"/api/utenti/{uid}/password",
                headers={"X-RankUp-Request":"1"},
                json={"password":"1234567"})
            assert weak.status_code==422,weak.text
            reset=admin.put(f"/api/utenti/{uid}/password",
                headers={"X-RankUp-Request":"1"},
                json={"password":"newpass8"})
            assert reset.status_code==200,reset.text
            assert user.get("/api/bandi").status_code==401
            assert _post(user,"/api/auth/login",json={
                "username":"collaboratore","password":"abcdefgh"
            }).status_code==401
            new_login=_post(user,"/api/auth/login",json={
                "username":"collaboratore","password":"newpass8"
            })
            assert new_login.status_code==200,new_login.text
            assert user.get("/api/auth/status").json()["role"]=="user"
        with server.connect() as db:
            ops=db.execute("""
                SELECT azione FROM rankup_utenti_eventi
                WHERE utente_id=? ORDER BY id
            """,(uid,)).fetchall()
            assert [r["azione"] for r in ops]==[
                "Creato account operativo","Disattivato","Riattivato",
                "Password reimpostata"
            ]


def test_persistent_users_survive_restart_but_sessions_do_not(tmp_path,monkeypatch):
    server,admin=_empty_setup(tmp_path,monkeypatch)
    with admin:
        registered=_post(admin,"/api/utenti",json={
            "username":"persistente","password":"00000000"
        })
        assert registered.status_code==200,registered.text
        with TestClient(server.app) as user:
            assert _post(user,"/api/auth/login",json={
                "username":"persistente","password":"00000000"
            }).status_code==200
            cookie=user.cookies.get("rankup_session")
            assert cookie
            importlib.reload(server)
            with TestClient(server.app) as after_restart:
                assert after_restart.get("/api/bandi",cookies={
                    "rankup_session":cookie
                }).status_code==401
                again=_post(after_restart,"/api/auth/login",json={
                    "username":"persistente","password":"00000000"
                })
                assert again.status_code==200,again.text
                assert again.json()["role"]=="user"


def test_user_management_navigation_is_admin_only():
    path=Path(__file__).resolve().parents[1]/"rankup"/"app"/"static"/"index.html"
    html=path.read_text(encoding="utf-8")
    assert 'data-page="utenti" id="admin-users-link" style="display:none"' in html
    assert 'state.role==="admin"?"flex":"none"' in html
    assert 'if(state.page==="utenti")return state.role==="admin"?renderUtenti()' in html
    assert 'type="password" autocomplete="new-password"' not in html or "new-user-password" in html
    assert 'minlength="8"' in html
    assert "Gestione account RankUp" in html
    assert "Acquisisci manualmente" in html


def test_delete_user_revokes_access_and_preserves_audit(tmp_path,monkeypatch):
    server,admin=_empty_setup(tmp_path,monkeypatch)
    with admin:
        created=_post(admin,"/api/utenti",json={
            "username":"eliminabile","password":"password8"
        })
        assert created.status_code==200,created.text
        uid=created.json()["utente"]["id"]
        with TestClient(server.app) as user:
            logged=_post(user,"/api/auth/login",json={
                "username":"eliminabile","password":"password8"
            })
            assert logged.status_code==200,logged.text
            assert user.delete(f"/api/utenti/{uid}",headers={"X-RankUp-Request":"1"}).status_code==403
            removed=admin.delete(f"/api/utenti/{uid}",headers={"X-RankUp-Request":"1"})
            assert removed.status_code==200,removed.text
            assert removed.json()["eliminato"] is True
            assert user.get("/api/bandi").status_code==401
            assert _post(user,"/api/auth/login",json={
                "username":"eliminabile","password":"password8"
            }).status_code==401
        assert admin.get("/api/utenti").json()["utenti"]==[]
        assert admin.delete(f"/api/utenti/{uid}",headers={"X-RankUp-Request":"1"}).status_code==404
        assert admin.put(f"/api/utenti/{uid}/stato",
            headers={"X-RankUp-Request":"1"},json={"attivo":True}).status_code==404
        with server.connect() as db:
            record=db.execute("SELECT attivo,eliminato FROM rankup_utenti WHERE id=?",(uid,)).fetchone()
            assert record["attivo"]==0 and record["eliminato"]==1
            actions=[x["azione"] for x in db.execute(
                "SELECT azione FROM rankup_utenti_eventi WHERE utente_id=? ORDER BY id",(uid,))]
            assert actions==["Creato account operativo","Eliminato (storico conservato)"]


def test_dashboard_hosts_pica_import_and_assessment_flags():
    path=Path(__file__).resolve().parents[1]/"rankup"/"app"/"static"/"index.html"
    html=path.read_text(encoding="utf-8")
    assert 'data-page="bandi"' not in html
    assert 'id="pica-file"' in html
    assert '$("preview-btn").onclick=preview' in html
    assert "NOT PASSED" in html and "PASSED" in html
    assert "data-user-delete" in html


def test_assignment_ente_requires_admin_and_source(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        data=admin.get("/api/bandi/26-02/incarichi")
        assert data.status_code==200,data.text
        assignment=data.json()["incarichi"][0]
        key=assignment["key"]
        uri=f"/api/bandi/26-02/incarichi/{key}/ente"
        bad=admin.put(uri,headers={"X-RankUp-Request":"1"},json={
            "ente_richiesto":"AOUI","fonte":""})
        assert bad.status_code==422,bad.text
        saved=admin.put(uri,headers={"X-RankUp-Request":"1"},json={
            "ente_richiesto":"AOUI","fonte":"Riga verificata dell'allegato ufficiale"})
        assert saved.status_code==200,saved.text
        found=admin.get("/api/bandi/26-02/incarichi").json()["incarichi"]
        assert next(r for r in found if r["key"]==key)["ente_richiesto"]=="AOUI"
        candidates=admin.get("/api/bandi/26-02/candidature")
        assert candidates.status_code==200,candidates.text
        assert "verifica_ente" in candidates.json()["candidature"][0]
        with server.connect() as db:
            r=db.execute("SELECT confermato_da FROM incarichi_ente_requisiti WHERE key_incarico=?",(key,)).fetchone()
            assert r["confermato_da"]=="test-admin"


def test_ente_check_is_conservative():
    from ente_check import evaluate_ente
    assert evaluate_ente("AOUI","dipendente",
        "Azienda Ospedaliera Universitaria Integrata di Verona","")["stato"]=="COERENTE (PRELIMINARE)"
    assert evaluate_ente("ASUIT","dipendente",
        "Azienda Ospedaliera Universitaria Integrata di Verona","")["stato"]=="DA VERIFICARE"
    assert evaluate_ente("SSN","","Studio privato non convenzionato","")["stato"]=="DA VERIFICARE"
    assert evaluate_ente("UNITN","","Università di Trento","")["stato"]=="DA VERIFICARE"
    assert evaluate_ente("NESSUNA RISERVA","","","")["stato"]=="NESSUNA RISERVA"
    assert evaluate_ente(None,"","","")["stato"]=="DA CONFIGURARE"
