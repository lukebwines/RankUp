"""Recupero assistito dei PDF appiattiti: solo dati sintetici."""
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from test_pdf_import import _setup, _preview, _confirm, _post, _complete_operator_onboarding
from pdf_import import visible_rankup_ids


def _static_pdf(marker: str, *, score="2", outcome="CANDIDATO IDONEO") -> bytes:
    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=(595, 842))
    pdf.drawString(30, 800, "SCHEDA SINTETICA DI COLLAUDO NON UTILIZZABILE COME ATTO")
    pdf.drawString(30, 770, f"TOTALE: {score}")
    pdf.drawString(30, 745, outcome)
    if marker:
        pdf.drawString(30, 36, marker)
    pdf.showPage()
    pdf.save()
    return out.getvalue()


def _marker(doc):
    fields = PdfReader(BytesIO(doc)).get_fields() or {}
    return str(fields["RANKUP-ID"]["/V"])


def test_flattened_pdf_identified_only_as_pending_and_recovered_by_admin(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        original = next(d for d in sheets.values()
                        if "P19" not in PdfReader(BytesIO(d)).get_fields())
        marker = _marker(original)
        flat = _static_pdf(marker)
        assert not PdfReader(BytesIO(flat)).get_fields()
        assert visible_rankup_ids(flat) == [marker]
        preview = _preview(client, [("appiattita.pdf", flat)])
        item = preview["schede"][0]
        assert item["stato"] == "da_verificare", item
        assert item["id_pica"] == marker.rsplit("-", 2)[1]
        assert item["punteggio"] is None
        assert item["decisione"] == "da_verificare"
        assert "identificativo testuale riconosciuto" in item["motivo"]
        _confirm(client, preview["token"])
        record = client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
        assert record["candidatura_id"] == item["candidatura_id"]
        response = _post(client,
            f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
            json={"candidatura_id": record["candidatura_id"],
                  "esito": "CANDIDATO IDONEO",
                  "punteggio": "2", "nota_commissione": "",
                  "motivazione_tecnica": "PDF appiattito, esito e punteggio verificati visivamente.",
                  "documento_verificato": True})
        assert response.status_code == 200, response.text
        assert response.json()["punteggio"] == 2
        assert client.get(
            f"/api/bandi/26-02/valutazioni/{record['id']}/documento"
        ).content == flat
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf_eventi").fetchone()[0] == 1


def test_flattened_pdf_unknown_id_is_not_accepted_manually(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        original = next(d for d in sheets.values()
                        if "P19" not in PdfReader(BytesIO(d)).get_fields())
        marker = _marker(original)
        flat = _static_pdf(marker[:-10] + "0000000000")
        preview = _preview(client, [("id_sbagliato.pdf", flat)])
        item = preview["schede"][0]
        assert item["stato"] == "da_verificare"
        assert item["candidatura_id"] is None
        _confirm(client, preview["token"])
        record = client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
        with server.connect() as db:
            candidate_id = db.execute("SELECT id FROM candidature WHERE stato='Attiva' LIMIT 1").fetchone()[0]
        response = _post(client,
            f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
            json={"candidatura_id": candidate_id,
                  "esito": "CANDIDATO IDONEO",
                  "punteggio": "2", "nota_commissione": "",
                  "motivazione_tecnica": "Documento volutamente alterato per collaudo.",
                  "documento_verificato": True})
        assert response.status_code == 409, response.text
        assert "RANKUP-ID" in response.text
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf WHERE stato='Acquisita'").fetchone()[0] == 0


def test_flattened_pdf_without_visible_id_cannot_be_approved(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        flat = _static_pdf("")
        preview = _preview(client, [("id_assente.pdf", flat)])
        assert preview["schede"][0]["candidatura_id"] is None
        _confirm(client, preview["token"])
        record = client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
        with server.connect() as db:
            candidate_id = db.execute("SELECT id FROM candidature WHERE stato='Attiva' LIMIT 1").fetchone()[0]
        response = _post(client,
            f"/api/bandi/26-02/valutazioni/{record['id']}/manuale",
            json={"candidatura_id": candidate_id,
                  "esito": "CANDIDATO IDONEO",
                  "punteggio": "2", "nota_commissione": "",
                  "motivazione_tecnica": "Documento senza identificativo per collaudo.",
                  "documento_verificato": True})
        assert response.status_code == 409, response.text


def _operator(server, admin_client):
    created=_post(admin_client,"/api/utenti",json={
        "username":"operatore_pdf_test", "password":"Password8!"})
    assert created.status_code==200,created.text
    other=TestClient(server.app)
    login=_post(other,"/api/auth/login",json={
        "username":"operatore_pdf_test","password":"Password8!"})
    assert login.status_code==200,login.text
    _complete_operator_onboarding(other,"operatore_pdf_test","Password8!")
    assert other.get("/api/auth/status").json()["role"]=="user"
    return other


def _start_flattened_case(client, sheets):
    original=next(d for d in sheets.values()
                  if "P19" not in PdfReader(BytesIO(d)).get_fields())
    flat=_static_pdf(_marker(original))
    preview=_preview(client,[("pdf_senza_campi.pdf",flat)])
    assert preview["riepilogo"]["da_verificare"]==1
    _confirm(client,preview["token"])
    doc=client.get("/api/bandi/26-02/valutazioni").json()["archivio"][0]
    assert doc["candidatura_id"] is not None
    return doc,flat


def _proposal_data(candidate_id):
    return {
        "candidatura_id":candidate_id,"esito":"CANDIDATO IDONEO",
        "punteggio":"2","nota_commissione":"Nota della commissione di prova",
        "motivazione_tecnica":"PDF appiattito, trascrizione verificata dall'operatore",
        "documento_verificato":True}


def test_operator_proposal_opens_one_admin_alert_and_never_updates_ranking(tmp_path,monkeypatch):
    server,admin,sheets=_setup(tmp_path,monkeypatch)
    with admin:
        doc,flat=_start_flattened_case(admin,sheets)
        with _operator(server,admin) as operator:
            target=f"/api/bandi/26-02/valutazioni/{doc['id']}/proposta"
            proposed=_post(operator,target,json=_proposal_data(doc["candidatura_id"]))
            assert proposed.status_code==200,proposed.text
            pid=proposed.json()["proposta_id"]
            duplicate=_post(operator,target,json=_proposal_data(doc["candidatura_id"]))
            assert duplicate.status_code==409
            assert operator.get("/api/bandi/26-02/valutazioni/proposte").json()["proposte"][0]["stato"]=="in_attesa"
            assert operator.get("/api/bandi/26-02/valutazioni").json()["totale_acquisite"]==0
            denied=_post(operator,
                f"/api/bandi/26-02/valutazioni/proposte/{pid}/approva",
                json={"documento_verificato":True})
            assert denied.status_code==403
            with server.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM segnalazioni WHERE autore=? AND stato='aperta'",
                                  (server.SYSTEM_ALERT_ACTOR,)).fetchone()[0]==1
                assert db.execute("SELECT COUNT(*) FROM valutazioni_pdf WHERE stato='Acquisita'").fetchone()[0]==0
            alert=admin.get("/api/bandi/26-02/segnalazioni").json()["segnalazioni"][0]
            assert "Recupero PDF" in alert["titolo"]
            # Il generico menu Segnalazioni non puo' chiudere una pratica pendente.
            close=admin.put(f"/api/bandi/26-02/segnalazioni/{alert['id']}",
                    headers={"X-RankUp-Request":"1"},
                    json={"stato":"risolta","assegnato_a":alert["assegnato_a"],
                          "revisione":alert["revisione"]})
            assert close.status_code==409,close.text
            without_check=_post(admin,
                f"/api/bandi/26-02/valutazioni/proposte/{pid}/approva",
                json={"documento_verificato":False})
            assert without_check.status_code==400
            approved=_post(admin,
                f"/api/bandi/26-02/valutazioni/proposte/{pid}/approva",
                json={"documento_verificato":True})
            assert approved.status_code==200,approved.text
            assert admin.get("/api/bandi/26-02/valutazioni").json()["totale_acquisite"]==1
            assert operator.get("/api/bandi/26-02/valutazioni/proposte").json()["proposte"][0]["stato"]=="approvata"
            with server.connect() as db:
                assert db.execute("SELECT stato FROM segnalazioni WHERE id=?",
                                  (alert["id"],)).fetchone()[0]=="risolta"
            assert admin.get(f"/api/bandi/26-02/valutazioni/{doc['id']}/documento").content==flat


def test_admin_rejection_closes_alert_without_score_and_operator_can_resubmit(tmp_path,monkeypatch):
    server,admin,sheets=_setup(tmp_path,monkeypatch)
    with admin:
        doc,_=_start_flattened_case(admin,sheets)
        with _operator(server,admin) as operator:
            endpoint=f"/api/bandi/26-02/valutazioni/{doc['id']}/proposta"
            response=_post(operator,endpoint,json=_proposal_data(doc["candidatura_id"]))
            assert response.status_code==200
            pid=response.json()["proposta_id"]
            rejected=_post(admin,f"/api/bandi/26-02/valutazioni/proposte/{pid}/respingi",
                json={"motivazione_rifiuto":"Il totale non corrisponde al PDF originale."})
            assert rejected.status_code==200,rejected.text
            assert admin.get("/api/bandi/26-02/valutazioni").json()["totale_acquisite"]==0
            own=operator.get("/api/bandi/26-02/valutazioni/proposte").json()["proposte"][0]
            assert own["stato"]=="respinta" and own["motivo_rifiuto"]
            with server.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM segnalazioni WHERE stato<>'risolta' AND autore=?",
                                  (server.SYSTEM_ALERT_ACTOR,)).fetchone()[0]==0
            retry=_post(operator,endpoint,json=_proposal_data(doc["candidatura_id"]))
            assert retry.status_code==200,retry.text
            assert retry.json()["proposta_id"]!=pid
