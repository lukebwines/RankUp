"""Collaudo del report istruttorio PDF v1.0.11.

Test con dati sintetici, senza attribuire fatti a candidati reali.
"""
from io import BytesIO

from pypdf import PdfReader

from ai_ente_report import render_ente_report_pdf


def _sample(i,esito="DA APPROFONDIRE",name=None):
    label=str(i).zfill(3)
    return {
        "id_pica":"TEST"+label,
        "cognome":name or "COGNOME"+label,
        "nome":"NOME"+label,
        "cds":"CORSO SINTETICO "+str(i%3),
        "sede":"SEDE SINTETICA",
        "insegnamento":"METODOLOGIA PROFESSIONALE E ORGANIZZAZIONE DELLE ATTIVITA FORMATIVE",
        "requisito":"SSN",
        "categoria_ente":"STRUTTURA SANITARIA (SSN DA VERIFICARE)",
        "categoria_rapporto":"RAPPORTO DIPENDENTE DICHIARATO",
        "esito":esito,
        "motivo":"Ente da identificare con documentazione ufficiale.",
        "verifiche":"Controllare la denominazione ufficiale dell'organizzazione e la natura giuridica.",
        "ai":{
            "motivo":"Ente da identificare con documentazione ufficiale.",
            "verifiche":"Controllare la denominazione ufficiale dell'organizzazione e la natura giuridica.",
        },
    }


def _text(pdf):
    return "\n".join(page.extract_text() or "" for page in pdf.pages)


def test_report_new_layout_is_landscape_and_includes_candidate_course_and_summary():
    data=[_sample(i) for i in range(1,18)]
    totals={"DA APPROFONDIRE":17,"ENTE COERENTE":21,"NON APPLICABILE":55}
    body=render_ente_report_pdf("26-TEST","Tutti i CdS","Tutte le sedi",
                                data,totals,"DA APPROFONDIRE")
    pdf=PdfReader(BytesIO(body))
    assert pdf.pages
    assert float(pdf.pages[0].mediabox.width)>float(pdf.pages[0].mediabox.height)
    text=_text(pdf)
    assert "Totale ambito" in text
    assert "93" in text
    assert "Nel PDF" in text
    assert "17" in text
    assert "55" in text
    assert "21" in text
    assert "COGNOME001 NOME001" in text
    assert "TEST001" in text
    assert "CORSO SINTETICO" in text
    assert "Requisito / ente" in text
    assert "Rapporto dichiarato" in text
    assert "Posizioni da approfondire" in text
    assert "Controllo suggerito:" in text
    assert "Nessun esito determina" not in text  # La formula e' differente.
    # Ogni pagina deve avere intestazione e numerazione.
    for number,page in enumerate(pdf.pages,1):
        t=page.extract_text()
        assert "RANKUP" in t
        assert "Bando: 26-TEST" in t
        assert "Pagina "+str(number) in t
    # Il suggerimento AI identico alla verifica NON deve comparire due volte.
    assert "Ulteriore indicazione AI:" not in text


def test_report_filtered_counts_still_show_full_scope_without_extra_rows():
    item=_sample(1,esito="DA APPROFONDIRE")
    data=[item]
    body=render_ente_report_pdf("TEST-01","CdS di prova","Sede di prova",
                                data,{"DA APPROFONDIRE":1,
                                      "ENTE COERENTE":10,
                                      "NON APPLICABILE":5},
                                "DA APPROFONDIRE")
    text=_text(PdfReader(BytesIO(body)))
    assert "16" in text
    assert "1" in text
    assert "CdS di prova" in text
    assert "Sede di prova" in text


def test_report_missing_names_use_explicit_verification_marker():
    item=_sample(1)
    item["nome"]=""
    item["cognome"]=""
    item["cds"]=""
    body=render_ente_report_pdf("TEST-01","Tutti i CdS",
                                "Tutte le sedi",[item])
    text=_text(PdfReader(BytesIO(body)))
    assert "[DA VERIFICARE]" in text


def test_report_long_text_wraps_without_losing_words_or_throwing():
    item=_sample(1)
    item["insegnamento"]="INSEGNAMENTO DI PROVA CON DENOMINAZIONE ESTREMAMENTE LUNGA "*7
    item["verifiche"]="Controllo sintetico su tutte le dichiarazioni. "*40
    body=render_ente_report_pdf("TEST-01","Tutti i CdS","Tutte le sedi",
                                [item])
    pdf=PdfReader(BytesIO(body))
    assert len(pdf.pages)>=1
    text=_text(pdf)
    assert "INSEGNAMENTO DI PROVA" in text
    assert "Controllo sintetico" in text


def test_report_endpoint_admin_only_and_includes_names_from_pica(tmp_path,monkeypatch):
    from test_ai_queue_v109 import _prepare,HEADERS
    from fastapi.testclient import TestClient
    from test_pdf_import import _post
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        originals=client.get("/api/bandi/26-02/candidature").json()["candidature"]
        expected={(r["id_pica"],r["cognome"],r["nome"]) for r in originals}
        response=client.get("/api/admin/ai/report.pdf",params={"code":"26-02"})
        assert response.status_code==200,response.text
        assert response.headers["content-type"].startswith("application/pdf")
        assert "attachment" in response.headers["content-disposition"]
        assert "RANKUP_ENTE_26_02" in response.headers["content-disposition"]
        filename=response.headers["content-disposition"].split('filename="')[-1].strip('"')
        assert len(filename)<85
        pdf=PdfReader(BytesIO(response.content))
        assert float(pdf.pages[0].mediabox.width)>float(pdf.pages[0].mediabox.height)
        text=_text(pdf)
        for pid,cognome,nome in expected:
            assert str(pid) in text
            assert (str(cognome)+" "+str(nome)).strip() in text
        created=_post(client,"/api/utenti",json={
            "username":"operatore-report","password":"PasswordTest1234!"})
        assert created.status_code==200,created.text
        with TestClient(server.app) as user:
            assert _post(user,"/api/auth/login",json={
                "username":"operatore-report","password":"PasswordTest1234!"}).status_code==200
            forbidden=user.get("/api/admin/ai/report.pdf",params={"code":"26-02"})
            assert forbidden.status_code==403
