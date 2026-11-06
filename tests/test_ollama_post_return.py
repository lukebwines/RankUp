"""La verifica IA occupazionale si avvia solo da schede restituite e solo come Admin."""
from io import BytesIO
from fastapi.testclient import TestClient
from pypdf import PdfReader
from test_pdf_import import _setup, _edit, _post, _preview, _confirm


def test_admin_only_after_returned_sheet(tmp_path, monkeypatch):
    server, admin, generated = _setup(tmp_path, monkeypatch)
    with admin:
        assert admin.post("/api/bandi/26-02/candidature/ai/anteprima",
                          headers={"X-RankUp-Request":"1"},json={}).status_code == 404
        # Senza schede restituite non esiste un controllo autorizzabile.
        not_returned = _post(admin,"/api/bandi/26-02/valutazioni/999/ai/occupazione",json={})
        assert not_returned.status_code == 404

        first = next(v for v in generated.values()
                     if "P19" not in (PdfReader(BytesIO(v)).get_fields() or {}))
        returned = _edit(first,{"P01":"2","TOT":"2",
                                "STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        preview = _preview(admin,[("restituita.pdf",returned)])
        assert _confirm(admin,preview["token"])["acquisite"] == 1
        archive = admin.get("/api/bandi/26-02/valutazioni").json()["archivio"]
        document_id = next(x["id"] for x in archive if x["stato"]=="Acquisita")

        # La risposta resta di sola lettura e non altera l'esito della commissione.
        changed = admin.put("/api/pica/tipologie",
                            headers={"X-RankUp-Request":"1"},
                            json={"chiave":"contratto esterno oneroso CSA",
                                  "requisito":"SSN","motivo":"Caso sintetico test"})
        assert changed.status_code == 200
        request_path=f"/api/bandi/26-02/valutazioni/{document_id}/ai/occupazione"
        with TestClient(server.app) as operator:
            from test_users import _post as post
            user = _post(admin,"/api/utenti",
                         json={"username":"operatore_test","password":"PasswordTest123"})
            assert user.status_code==200
            login=_post(operator,"/api/auth/login",
                        json={"username":"operatore_test","password":"PasswordTest123"})
            assert login.status_code==200
            assert _post(operator,request_path,json={}).status_code==403

        captured={}
        def fake_review(req,occ,company,profile,endpoint):
            captured.update(req=req,occ=occ,company=company,profile=profile)
            return {"stato":"DA VERIFICARE","motivo":"Verifica documentale necessaria",
                    "evidenza":"","modello_locale":True}
        monkeypatch.setattr(server,"review_occupation",fake_review)
        monkeypatch.setattr(server,"_ollama_settings",lambda:"http://127.0.0.1:11434")
        answer=_post(admin,request_path,json={})
        assert answer.status_code==200,answer.text
        assert answer.json()["risultato"]["stato"]=="DA VERIFICARE"
        assert captured["req"]=="SSN"
        after=admin.get("/api/bandi/26-02/valutazioni").json()["archivio"]
        assert next(x for x in after if x["id"]==document_id)["decisione"]=="idoneo"
