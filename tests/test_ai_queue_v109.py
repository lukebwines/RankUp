"""RankUP v1.0.9: prompt personalizzabile, coda durevole e filtri.

I nominativi e i codici dell'esempio sono sintetici; nessuna chiamata HTTP reale.
"""
import json
from io import BytesIO

from pypdf import PdfReader
from test_pdf_import import _setup, _post

HEADERS={"X-RankUp-Request":"1"}


def _prepare(tmp_path,monkeypatch):
    monkeypatch.setenv("RANKUP_DISABLE_AI_WORKER","1")
    server,client,_=_setup(tmp_path,monkeypatch)
    response=client.put("/api/admin/ai/config",headers=HEADERS,json={
        "model":"openai/gpt-oss-120b","api_key":"groq_fittizia_non_vera_123456789"})
    assert response.status_code==200,response.text
    # Per il collaudo PICA, e' dichiarata un'occupazione solo sintetica.
    with server.connect() as db:
        rows=db.execute("SELECT id,source_json FROM candidature").fetchall()
        for row in rows:
            obj=json.loads(row["source_json"] or "{}")
            obj.update({"occupazione":"Dipendente",
                        "azienda_ente":"AOUI Verona",
                        "profilo_professionale":"Dirigente"})
            db.execute("UPDATE candidature SET source_json=? WHERE id=?",
                       (json.dumps(obj,ensure_ascii=False),row["id"]))
        db.commit()
    candidates=client.get("/api/bandi/26-02/candidature").json()["candidature"]
    for key in {item["incarico_key"] for item in candidates}:
        response=client.put("/api/bandi/26-02/incarichi/"+key+"/ente",
            headers=HEADERS,json={"ente_richiesto":"SSN",
              "fonte":"Riferimento interamente sintetico per il collaudo"})
        assert response.status_code==200,response.text
    return server,client


def test_prompt_editable_admin_and_ai_produces_institutional_coherence(tmp_path,monkeypatch):
    from ai_ente_rules import ENTE_COERENTE,DA_APPROFONDIRE
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        config=client.get("/api/admin/ai/istruzioni")
        assert config.status_code==200,config.text
        default=config.json()["predefinito"]
        assert "AOUI" in default and "ASUIT" in default and "SSN" in default
        prompt=default+"\nNon dichiarare mai l'idoneita' dei candidati."
        saved=client.put("/api/admin/ai/istruzioni",headers=HEADERS,
                         json={"testo":prompt})
        assert saved.status_code==200,saved.text
        assert saved.json()["testo"]==prompt
        assert (tmp_path/"groqcloud_guidance.txt").stat().st_mode & 0o077==0
        rows=client.get("/api/admin/ai/verifiche",params={"code":"26-02"})
        assert rows.status_code==200,rows.text
        data=rows.json()
        assert data["totale"]==3
        # Cognome e nome provengono dalle candidature PICA gia' importate.
        source=client.get("/api/bandi/26-02/candidature").json()["candidature"]
        expected={(str(c["id"]),str(c["incarico_key"])):
                  (c["cognome"],c["nome"]) for c in source}
        for row in data["casi"]:
            assert (row["cognome"],row["nome"])==expected[
                (str(row["id"]),str(row["incarico_key"]))]
        # Prima dell'AI RankUP non decide piu' SSN/AOUI localmente.
        assert data["conteggi"][DA_APPROFONDIRE]==3
        job=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert job.status_code==200,job.text
        assert job.json()["totale_candidature"]==3
        outbound=[]
        def fake_send(key,model,payload,guidance=None):
            assert key=="groq_fittizia_non_vera_123456789"
            assert model=="openai/gpt-oss-120b"
            assert guidance==prompt
            assert set(payload)=={"requisito_ente","denominazione_ente_dichiarato"}
            assert payload["requisito_ente"]=="SSN"
            assert payload["denominazione_ente_dichiarato"]=="AOUI VERONA"
            assert "1001" not in str(payload)
            assert "CODICEFISCALE" not in str(payload)
            outbound.append(dict(payload))
            return {"indizio":"INDIZI COERENTI","motivo":"Ente coerente a livello istituzionale",
                    "verifiche":"Verificare separatamente il rapporto di lavoro",
                    "origine":"groqcloud","token_usati":125}
        for i in range(4):
            assert server.ai_process_one(server.connect,server._ai_worker_candidates,
                fake_send,server.GROQ.key,server.GROQ.limits()) is True
        assert len(outbound)==1  # Tre categorie uguali, una sola chiamata remota
        results=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()
        assert results["conteggi"][ENTE_COERENTE]==3
        assert all(r["ai"]["indizio"]=="INDIZI COERENTI" for r in results["casi"])
        assert all("rapporto" in r["verifiche"].lower() for r in results["casi"])
        status=client.get("/api/admin/ai/lavori",params={"code":"26-02"}).json()
        assert status["lavoro"]["stato"]=="completata"
        assert status["lavoro"]["chiamate"]==1
        assert status["lavoro"]["riutilizzati"]==2
        assert status["lavoro"]["token"]==125
        report=client.get("/api/admin/ai/report.pdf",params={"code":"26-02","esito":ENTE_COERENTE})
        assert report.status_code==200,report.text
        pdf=PdfReader(BytesIO(report.content))
        report_text="\n".join(p.extract_text() or "" for p in pdf.pages)
        assert "ID PICA" in report_text and "ENTE COERENTE" in report_text
        # Nuovo lavoro con stessa versione del prompt: 100% cache.
        duplicate=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert duplicate.status_code==200,duplicate.text
        repeated=client.get("/api/admin/ai/lavori",params={"code":"26-02"}).json()["lavoro"]
        assert repeated["riutilizzati"]==3
        assert repeated["da_esaminare"]==0
        # Il cambio delle istruzioni NON riscrive gli esiti, ma cambia l'hash cache.
        new=client.put("/api/admin/ai/istruzioni",headers=HEADERS,json={
            "testo":prompt+"\nAggiornamento esclusivamente sintetico."})
        assert new.status_code==200,new.text
        refreshed=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()
        assert refreshed["conteggi"][DA_APPROFONDIRE]==3
        assert all(not row["ai"] for row in refreshed["casi"])


def test_unauthorized_prompt_and_queue_are_admin_only(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        response=_post(client,"/api/utenti",json={
            "username":"operatore-ai","password":"PasswordUtenteTest123!"})
        assert response.status_code==200,response.text
        with TestClient(server.app) as user:
            assert _post(user,"/api/auth/login",json={
                "username":"operatore-ai","password":"PasswordUtenteTest123!"}).status_code==200
            assert user.get("/api/admin/ai/config").status_code==403
            assert user.get("/api/admin/ai/limiti").status_code==403
            assert user.get("/api/admin/ai/modelli").status_code==403
            assert user.get("/api/admin/ai/report.pdf?code=26-02").status_code==403
            assert user.get("/api/admin/ai/istruzioni").status_code==403
            assert user.get("/api/admin/ai/verifiche?code=26-02").status_code==403
            assert user.get("/api/admin/ai/lavori?code=26-02").status_code==403
            assert _post(user,"/api/admin/ai/lavori",json={
                "code":"26-02","conferma_servizio":True}).status_code==403


def test_quota_429_pause_resume_and_progress_persist_after_restart(tmp_path,monkeypatch):
    from ai_groq import GroqLimit
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        job=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert job.status_code==200,job.text
        jobid=job.json()["lavoro"]["id"]
        def no_quota(*args,**kwargs):
            raise GroqLimit(65)
        assert server.ai_process_one(server.connect,server._ai_worker_candidates,
            no_quota,server.GROQ.key,server.GROQ.limits()) is True
        paused=client.get("/api/admin/ai/lavori",params={"code":"26-02"}).json()["lavoro"]
        assert paused["stato"]=="attesa_quota"
        assert 0<=paused["riprende_tra"]<=65
        assert paused["completati"]==0
        response=_post(client,f"/api/admin/ai/lavori/{jobid}/sospendi")
        assert response.status_code==200,response.text
        assert response.json()["lavoro"]["stato"]=="sospesa"
        # Nuovo avvio client senza ricreare la banca dati.
        response=client.get("/api/admin/ai/lavori",params={"code":"26-02"})
        assert response.json()["lavoro"]["stato"]=="sospesa"
        response=_post(client,f"/api/admin/ai/lavori/{jobid}/riprendi")
        assert response.status_code==200,response.text
        assert response.json()["lavoro"]["stato"]=="in_coda"
        canceled=_post(client,f"/api/admin/ai/lavori/{jobid}/annulla")
        assert canceled.status_code==200
        assert canceled.json()["lavoro"]["stato"]=="annullata"



def test_interfaccia_ai_javascript_is_syntactically_valid(tmp_path):
    """Evita regressioni di sintassi del nuovo grande pannello HTML/JS."""
    import re
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node=shutil.which("node")
    if not node:
        pytest.skip("Node non disponibile nell'ambiente di test")
    html=(Path(__file__).resolve().parents[1]/
          "rankup/app/static/index.html").read_text("utf-8")
    matches=re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>",html)
    assert matches
    script=tmp_path/"rankup_ui.js"
    script.write_text("\n".join(matches),encoding="utf-8")
    result=subprocess.run([node,"--check",str(script)],capture_output=True,
                          text=True,timeout=15)
    assert result.returncode==0,result.stderr



def test_ai_menu_icons_resolve_and_names_only_appear_locally():
    """Entrambi i nuovi menu hanno icone valide; nome protetto nell'UI Admin."""
    import re
    from pathlib import Path
    from ai_groq import prepare_minimal_payload
    page=(Path(__file__).resolve().parents[1]/
          "rankup/app/static/index.html").read_text("utf-8")
    icons=set(re.findall(r'<symbol\s+id="([^"]+)"',page))
    for key in ("admin-ai-link","band-ai-link"):
        match=re.search(r'<button[^>]*\bid="'+key+r'"[^>]*>(.*?)</button>',
                        page,re.DOTALL)
        assert match, key
        used=re.findall(r'<use\s+href="#([^"]+)"',match.group(1))
        assert len(used)==1,(key,used)
        assert used[0] in icons,(key,used[0])
    assert "Cerca candidato o ID PICA" in page
    assert "<th>Candidato</th>" in page
    assert "esc(candidato||'[DA VERIFICARE]')" in page
    assert "var candidato=[row.cognome,row.nome].filter(Boolean).join(' ')" in page
    # Non esporre i dati personali al provider.
    source={"id_pica":"ID-SINTETICO","cognome":"COGNOME_DI_PROVA",
            "nome":"NOME_DI_PROVA","ente_richiesto":"SSN",
            "azienda_ente":"AOUI Verona","occupazione":"Dipendente",
            "profilo_professionale":"Dirigente"}
    payload=prepare_minimal_payload(source)
    assert "COGNOME_DI_PROVA" not in str(payload)
    assert "NOME_DI_PROVA" not in str(payload)
    assert "ID-SINTETICO" not in str(payload)
