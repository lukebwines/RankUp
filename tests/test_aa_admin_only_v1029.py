"""Regressioni per creazione A.A. riservata all'Admin in RankUP 1.0.29.

Le annualità e i riferimenti sono esclusivamente dati sintetici di test.
"""
from pathlib import Path

from fastapi.testclient import TestClient
from test_pdf_import import _setup, _post


ROOT = Path(__file__).resolve().parents[1]


def _put_refs(client, data):
    return client.put(
        "/api/bandi/26-02/riferimenti",
        json=data, headers={"X-RankUp-Request": "1"},
    )


def test_only_admin_can_register_a_new_academic_year(tmp_path, monkeypatch):
    server, admin, _ = _setup(tmp_path, monkeypatch)
    with admin:
        baseline = _put_refs(admin, {
            "anno_accademico": "2026-2027",
            "expected_updated_at": None,
        })
        assert baseline.status_code == 200, baseline.text
        assert baseline.json()["riferimenti"]["anno_accademico"] == "2026-2027"

        created = _post(admin, "/api/utenti", json={
            "username": "operatore-aa-test",
            "password": "PasswordFittiziaTest2026!",
        })
        assert created.status_code == 200, created.text

        with TestClient(server.app) as user:
            login = _post(user, "/api/auth/login", json={
                "username": "operatore-aa-test",
                "password": "PasswordFittiziaTest2026!",
            })
            assert login.status_code == 200, login.text
            assert login.json()["role"] == "user"

            current = user.get("/api/bandi/26-02/riferimenti")
            assert current.status_code == 200
            values = current.json()["riferimenti"]

            forbidden = _put_refs(user, {
                "anno_accademico": "2027-2028",
                "expected_updated_at": values["aggiornato_il"],
            })
            assert forbidden.status_code == 403, forbidden.text
            assert "Admin" in forbidden.json()["detail"]

            after_denial = user.get("/api/bandi/26-02/riferimenti").json()["riferimenti"]
            assert after_denial["anno_accademico"] == "2026-2027"
            assert after_denial["aggiornato_il"] == values["aggiornato_il"]

            # Riferimenti e A.A. già presenti restano gestibili dall'operatore.
            permitted = _put_refs(user, {
                "anno_accademico": "2026-2027",
                "repertorio": "RIFERIMENTO SINTETICO",
                "expected_updated_at": values["aggiornato_il"],
            })
            assert permitted.status_code == 200, permitted.text
            assert permitted.json()["riferimenti"]["repertorio"] == "RIFERIMENTO SINTETICO"

            # La preparazione del nuovo bando è già riservata all'Admin.
            new_bando = _post(user, "/api/bandi", json={
                "codice": "CODICE-PROVA",
                "anno_accademico": "2027-2028",
            })
            assert new_bando.status_code == 403

        # L'Admin mantiene la possibilità di introdurre un nuovo A.A.
        current_admin = admin.get("/api/bandi/26-02/riferimenti").json()["riferimenti"]
        allowed = _put_refs(admin, {
            "anno_accademico": "2027-2028",
            "repertorio": "RIFERIMENTO SINTETICO",
            "expected_updated_at": current_admin["aggiornato_il"],
        })
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["riferimenti"]["anno_accademico"] == "2027-2028"


def test_ui_hides_new_year_button_for_operators_and_blocks_stale_selection():
    html = (ROOT / "rankup/app/static/index.html").read_text(encoding="utf-8")
    config = (ROOT / "rankup/config.yaml").read_text(encoding="utf-8")
    assert 'version: "1.0.29"' in config
    assert 'id="add-academic-year" class="add-year" type="button" style="display:none"' in html
    assert '$("add-academic-year").style.display=state.role==="admin"?"block":"none"' in html
    assert 'if(state.role!=="admin"){' in html
    assert 'function anniRegistrati()' in html
    assert 'if(state.role==="admin"&&annoValido(state.year))' in html
    assert '!anniRegistrati().includes(state.year)' in html
    assert '!anniRegistrati().includes(year)' in html
    assert "La creazione di nuovi anni accademici è riservata" in html
    assert "Solo l’Admin può creare un nuovo anno accademico." in html
