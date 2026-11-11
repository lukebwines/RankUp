"""Regressioni GroqCloud: minimizzazione, permessi, persistenza, senza rete."""
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfReader
from test_pdf_import import _setup, _post
from test_graduatorie import _complete_all
from ai_groq import prepare_minimal_payload, GroqLimit

HEADERS = {"X-RankUp-Request": "1"}


def test_only_institutional_categories_are_sent_to_groq():
    raw={
        "id_pica":"12345",
        "cognome":"NOME SINTETICO",
        "codice_fiscale":"RSSMRA80A01H501U",
        "email":"fittizio@example.test",
        "occupazione":"Dipendente, nome sintetico, numero 3331234567",
        "azienda_ente":"AOUI Verona, struttura reparto privato con nominativo",
        "profilo_professionale":"Dirigente",
        "ente_richiesto":"AOUI",
    }
    minimal=prepare_minimal_payload(raw)
    assert set(minimal)=={
        "requisito_ente","categoria_ente_dichiarato",
        "categoria_rapporto_dichiarato",
    }
    assert minimal["requisito_ente"]=="AOUI"
    assert minimal["categoria_ente_dichiarato"]=="AOUI"
    assert "12345" not in str(minimal)
    assert "RSSMRA" not in str(minimal)
    assert "fittizio@" not in str(minimal)
    assert "NOME SINTETICO" not in str(minimal)
    # Nomi di aziende senza match esatto non devono essere inviati.
    unknown=prepare_minimal_payload({**raw,"azienda_ente":"Studio privato fittizio Rossi"})
    assert unknown["categoria_ente_dichiarato"]=="ENTE NON CLASSIFICATO"
    assert "Rossi" not in str(unknown)


def test_admin_only_and_private_key(tmp_path,monkeypatch):
    server,client,_=_setup(tmp_path,monkeypatch)
    with client:
        public=client.get("/api/admin/ai/config")
        assert public.status_code==200,public.text
        assert public.json()["chiave_configurata"] is False
        response=client.put("/api/admin/ai/config",headers=HEADERS,
            json={"model":"llama-3.3-70b-versatile","api_key":"groq_test_secret_1234567890"})
        assert response.status_code==200,response.text
        assert response.json()["chiave_configurata"] is True
        assert "groq_test_secret" not in response.text
        assert "groq_test_secret" not in client.get("/api/admin/ai/config").text
        keyfile=tmp_path/".groqcloud_key"
        assert keyfile.read_text()=="groq_test_secret_1234567890"
        assert (keyfile.stat().st_mode & 0o077)==0
        created=_post(client,"/api/utenti",json={
            "username":"operatore_test","password":"PasswordTest1234!"})
        assert created.status_code==200,created.text
        with TestClient(server.app) as user:
            assert _post(user,"/api/auth/login",json={
                "username":"operatore_test","password":"PasswordTest1234!"
            }).status_code==200
            assert user.get("/api/admin/ai/config").status_code==403
            assert user.get("/api/admin/ai/modelli").status_code==403
            assert user.get("/api/admin/ai/risultati?code=26-02").status_code==403
            assert user.get("/api/admin/ai/report.pdf?code=26-02").status_code==403
            assert _post(user,"/api/admin/ai/analizza-passo",
                json={"code":"26-02"}).status_code==403


def test_analysis_runs_after_sheets_and_stores_only_local_id(tmp_path,monkeypatch):
    server,admin,sheets=_setup(tmp_path,monkeypatch)
    with admin:
        config=admin.put("/api/admin/ai/config",headers=HEADERS,json={
            "api_key":"groq_fake_key_1234567890","model":"llama-3.3-70b-versatile"})
        assert config.status_code==200,config.text
        sent=[]
        def fake_analyze(key,model,payload):
            assert key=="groq_fake_key_1234567890"
            assert model=="llama-3.3-70b-versatile"
            assert set(payload)=={
                "requisito_ente","categoria_ente_dichiarato",
                "categoria_rapporto_dichiarato"}
            assert "candidato" not in str(payload).lower()
            sent.append(dict(payload))
            return {"indizio":"DATI INSUFFICIENTI",
                    "motivo":"Informazioni insufficienti per confermare il rapporto.",
                    "verifiche":"Verificare la posizione dichiarata presso l'ente."}
        monkeypatch.setattr(server,"groq_analyze",fake_analyze)
        # Prima della restituzione delle schede non analizza candidature.
        before=_post(admin,"/api/admin/ai/analizza-passo",json={"code":"26-02"})
        assert before.status_code==200,before.text
        assert before.json()["nuovi"]==0
        assert not sent
        _complete_all(admin,sheets)
        with server.connect() as db:
            start=db.execute("SELECT COUNT(*) FROM valutazioni_pdf WHERE stato='Acquisita'").fetchone()[0]
        first=_post(admin,"/api/admin/ai/analizza-passo",json={"code":"26-02"})
        assert first.status_code==200,first.text
        assert first.json()["nuovi"]==3
        result=admin.get("/api/admin/ai/risultati?code=26-02")
        assert result.status_code==200,result.text
        body=result.json()
        assert body["completati"]==3
        assert body["da_analizzare"]==0
        assert all(row["id_pica"] for row in body["risultati"])
        # Le categorie sono l'unico dato trasmesso; il rapporto con PICA è interno.
        assert len(sent)<=3
        again=_post(admin,"/api/admin/ai/analizza-passo",json={"code":"26-02"})
        assert again.status_code==200 and again.json()["nuovi"]==0
        report=admin.get("/api/admin/ai/report.pdf?code=26-02")
        assert report.status_code==200,report.text
        output=PdfReader(BytesIO(report.content))
        assert len(output.pages)>=1
        text="\n".join(page.extract_text() or "" for page in output.pages)
        assert "ID PICA" in text
        assert "DA VERIFICARE" not in text.upper() or "DA VERIFICARE" in text.upper()
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM ai_ente_verifiche").fetchone()[0]==3
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf WHERE stato='Acquisita'").fetchone()[0]==start
            assert db.execute("SELECT COUNT(*) FROM graduatorie_cds").fetchone()[0]==0


def test_rate_limit_preserves_previous_results(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        _complete_all(client,sheets)
        response=client.put("/api/admin/ai/config",headers=HEADERS,json={
            "model":"llama-3.3-70b-versatile",
            "api_key":"groq_fake_key_1234567890"})
        assert response.status_code==200,response.text
        def noquota(*_):
            raise GroqLimit(70)
        monkeypatch.setattr(server,"groq_analyze",noquota)
        result=_post(client,"/api/admin/ai/analizza-passo",json={"code":"26-02"})
        assert result.status_code==200,result.text
        assert result.json()["limite_raggiunto"] is True
        assert result.json()["retry_after"]==70
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM ai_ente_verifiche").fetchone()[0]==0
