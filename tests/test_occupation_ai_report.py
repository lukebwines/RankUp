"""Audit persistente Ollama e report PDF per CdS, solo amministratore."""
from io import BytesIO
import importlib

from pypdf import PdfReader
from fastapi.testclient import TestClient
from test_pdf_import import _setup, _post, _preview, _confirm, _edit


def test_report_and_persistent_ai_audit(tmp_path, monkeypatch):
    server,admin,sheets=_setup(tmp_path,monkeypatch)
    with admin:
        one=next(pdf for pdf in sheets.values()
                 if "P19" not in (PdfReader(BytesIO(pdf)).get_fields() or {}))
        # Le due diciture finali non devono essere in nessun modello.
        for pdf in sheets.values():
            text=PdfReader(BytesIO(pdf)).pages[0].extract_text() or ""
            assert "Documento da compilare e sottoporre successivamente a firma digitale." not in text
            assert "Firma e protocollo sono gestiti fuori da RankUp." not in text

        doc=_edit(one,{"P01":"2","TOT":"2","STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"})
        preview=_preview(admin,[("restituita.pdf",doc)])
        assert _confirm(admin,preview["token"])["acquisite"]==1
        archive=admin.get("/api/bandi/26-02/valutazioni").json()["archivio"]
        record=next(a for a in archive if a["stato"]=="Acquisita")
        pdf_id=record["id"]
        cds=record["cds"]

        # La riserva del caso di collaudo deriva dalla rettifica approvata.
        changed=admin.put("/api/pica/tipologie",headers={"X-RankUp-Request":"1"},
                          json={"chiave":"contratto esterno oneroso CSA",
                                "requisito":"SSN","motivo":"Caso sintetico di prova"})
        assert changed.status_code==200,changed.text

        def fake_review(req,occ,company,profile,endpoint):
            assert req=="SSN"
            return {"stato":"DA VERIFICARE","motivo":"Rapporto attivo da documentare",
                    "evidenza":"","modello_locale":True}
        monkeypatch.setattr(server,"review_occupation",fake_review)
        monkeypatch.setattr(server,"_ollama_settings",lambda:"http://127.0.0.1:11434")
        endpoint=f"/api/bandi/26-02/valutazioni/{pdf_id}/ai/occupazione"
        for _ in range(2):
            answer=_post(admin,endpoint,json={})
            assert answer.status_code==200,answer.text

        history=admin.get("/api/bandi/26-02/valutazioni/ai/occupazione/storico")
        assert history.status_code==200
        assert history.json()["controlli"][str(pdf_id)]["numero_controlli"]==2
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_occupazione_ai").fetchone()[0]==2

        # Anche dopo il riavvio dell'app lo storico persiste.
        importlib.reload(server)
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_occupazione_ai").fetchone()[0]==2

        report=admin.get("/api/bandi/26-02/valutazioni/ai/occupazione/report.pdf",
                         params={"cds":cds})
        assert report.status_code==200,report.text[:300] if report.status_code!=200 else ""
        assert report.headers["content-type"]=="application/pdf"
        report_text="\n".join(x.extract_text() or "" for x in PdfReader(BytesIO(report.content)).pages)
        assert "Rapporto attivo da documentare" in report_text
        assert "contratto esterno oneroso CSA" in report_text
        assert "SSN" in report_text
        assert "Occupazione dichiarata" in report_text
        assert "Ente / azienda dichiarata" in report_text
        assert "controllo IA non eseguito" in report_text.lower() or "Controllo non eseguito" in report_text

        # Utente operativo non deve poter leggere storico, dati IA o report.
        created=_post(admin,"/api/utenti",
                      json={"username":"operatore_demo","password":"PasswordDemo123"})
        assert created.status_code==200
        with TestClient(server.app) as operator:
            login=_post(operator,"/api/auth/login",
                        json={"username":"operatore_demo","password":"PasswordDemo123"})
            assert login.status_code==200
            assert operator.get("/api/bandi/26-02/valutazioni/ai/occupazione/storico").status_code==403
            assert operator.get("/api/bandi/26-02/valutazioni/ai/occupazione/report.pdf",
                                params={"cds":cds}).status_code==403
            assert _post(operator,endpoint,json={}).status_code==403
        remaining=admin.get("/api/bandi/26-02/valutazioni").json()["archivio"]
        assert next(x for x in remaining if x["id"]==pdf_id)["decisione"]=="idoneo"
