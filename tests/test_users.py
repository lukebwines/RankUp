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
