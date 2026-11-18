"""Test sintetici CO Veneto: nessuna comunicazione reale, nessun invio esterno."""
import json
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from test_pdf_import import _setup, _post, _complete_operator_onboarding
from test_graduatorie import _complete_all
from fastapi.testclient import TestClient
from co_veneto import (
    CONFIG_FIELDS, CONFIG_DEFAULTS, CODICE_NUOVA_COMUNICAZIONE,
    VERSIONE_TRACCIATO, NS, academic_end, make_xml,
    defaults_from_pica, validate_worker, note_from_approval
)


def _fake_config():
    # Questi sono SOLTANTO valori sintetici di test.
    values={key:"VALORE SINTETICO" for key in CONFIG_FIELDS}
    values.update(CONFIG_DEFAULTS)
    values.update({
        "codice_fiscale":"00000000000",
        "denominazione":"DATORE SINTETICO DI COLLAUDO",
        "legale_cognome":"COGNOME TEST", "legale_nome":"NOME TEST",
        "legale_sesso":"F", "legale_cittadinanza":"000",
        "legale_comune_nascita":"L781", "legale_data_nascita":"1980-01-01",
        "sede_legale_comune":"L781", "sede_legale_cap":"00000",
        "sede_legale_indirizzo":"VIA DI COLLAUDO 1",
        "sede_legale_telefono":"00000000",
        "sede_lavoro_comune":"L781", "sede_lavoro_cap":"00000",
        "sede_lavoro_indirizzo":"VIA DI COLLAUDO 2",
        "sede_lavoro_telefono":"00000000",
        "settore_ateco":"85.40.20", "cls_settore":"A25",
        "email_delegato":"test@example.invalid",
        "pat_inail":"000000000",
        "qualifica_professionale":"2.6.1.2.3"
    })
    return values


def test_xml_source_only_and_no_presumed_worker_codes():
    assert academic_end("2026-2027")=="2027-09-30"
    assert academic_end("2026-2028")==""
    original={"nome":"NOME","cognome":"COGNOME",
              "codice_fiscale":"TEST000000000001",
              "data_nascita":"01/01/1990", "sesso":"F",
              "comune_nascita":"Verona", "cittadinanza":"Italia",
              "residenza_indirizzo":"VIA DI PROVA",
              "residenza_citta":"Verona", "residenza_cap":"37100"}
    default=defaults_from_pica(original)
    assert default["domicilio_indirizzo"]=="VIA DI PROVA"
    assert default["domicilio_comune_codice"]==""  # senza Stato o provincia verificati
    assert default["livello_istruzione"]=="70"
    assert default["nascita_comune_codice"]==""
    assert "codice catastale comune di nascita" in validate_worker(
        original, default, "2026-10-19","2027-09-30"
    )
    default.update(retribuzione="1000", nascita_comune_codice="L781",
                   domicilio_comune_codice="L781", livello_istruzione="70")
    raw=make_xml(_fake_config(), original, default,
                 "2026-10-19","2027-09-30",
                 when="2026-11-18T18:00:00")
    root=ET.fromstring(raw)
    assert root.attrib["codiceComunicazione"]==CODICE_NUOVA_COMUNICAZIONE
    assert root.attrib["versione"]==VERSIONE_TRACCIATO
    assert root.attrib["dataInvio"]=="2026-11-18T18:00:00"
    # Nello storico audit l'ora è timezone-aware, nel tracciato XML no.
    with_offset=make_xml(_fake_config(), original, default,
                         "2026-10-19","2027-09-30",
                         when="2026-11-18T18:00:00+01:00")
    assert ET.fromstring(with_offset).attrib["dataInvio"]=="2026-11-18T18:00:00"
    foreign=dict(default, cittadinanza_codice="999")
    assert any("cittadinanza estera" in p for p in
               validate_worker(original, foreign, "2026-10-19", "2027-09-30"))

    assert "protocollo" not in root.attrib
    assert root.find(".//{"+NS+"}IndirizzoLavoratore/{"+NS+"}Indirizzo").text=="VIA DI PROVA"
    assert root.find(".//{"+NS+"}InizioRapporto").attrib["TipologiaContrattuale"]=="B.03.00"
    assert root.find(".//{"+NS+"}InizioRapporto").attrib["dataFine"]=="2027-09-30"


def _approved(client):
    base="/api/bandi/26-02"
    refs=client.get(base+"/riferimenti").json()["riferimenti"]
    settings={key:refs.get(key) or "" for key in (
        "repertorio","protocollo","data_avviso","data_scadenza",
        "anno_accademico","testo_verbale_bando")}
    settings["anno_accademico"]="2026-2027"
    settings["expected_updated_at"]=refs["aggiornato_il"]
    response=client.put(base+"/riferimenti",headers={"X-RankUp-Request":"1"},json=settings)
    assert response.status_code==200,response.text
    sheets=client.get(base+"/schede/zip")
    assert sheets.status_code==200
    with ZipFile(BytesIO(sheets.content)) as folder:
        documents={p:folder.read(p) for p in folder.namelist() if not p.startswith("VB_")}
    _complete_all(client,documents)
    view=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
    body={"cds":view["cds"],"sede":view["sede"]}
    assert _post(client,base+"/verbali/graduatorie/genera-definitivo",json=body).status_code==200
    view=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
    draft=_post(client,base+"/graduatorie/approvazioni",
                json={"graduatorie_ids":[view["conferma"]["id"]]})
    assert draft.status_code==200,draft.text
    aid=draft.json()["id"]
    before=client.get(base+"/co-veneto/approvazioni/"+str(aid))
    assert before.status_code==200
    assert before.json()["attiva"] is False
    confirmed=_post(client,base+"/graduatorie/approvazioni/"+str(aid)+"/conferma",
                    json={"estremi_atto":"ATTO SINTETICO VERIFICATO SOLO PER TEST"})
    assert confirmed.status_code==200,confirmed.text
    return aid


def test_generation_lifecycle_role_lock_and_archival(tmp_path,monkeypatch):
    server,admin,_=_setup(tmp_path,monkeypatch)
    with admin:
        aid=_approved(admin)
        path="/api/bandi/26-02/co-veneto/approvazioni/"+str(aid)
        view=admin.get(path).json()
        assert view["attiva"] is True
        approvals=admin.get("/api/bandi/26-02/graduatorie/approvazioni").json()["approvazioni"]
        assert next(a for a in approvals if a["id"]==aid)["co_veneto_zip_stato"]=="non_generato"
        assert len(view["incarichi"])==2
        job=view["incarichi"][0]
        assert job["residenza_pica"]["indirizzo"]=="Via Roma 1"
        assert job["work"]["domicilio_indirizzo"]=="Via Roma 1"
        assert job["work"]["domicilio_comune_codice"]=="L781"
        assert job["work"]["note"].endswith("A.A. 2026/2027; BANDO 26-02")
        assert job["cds"] in job["work"]["note"]
        assert job["insegnamento"] in job["work"]["note"]
        assert job["work"]["livello_istruzione"]=="70"
        early=_post(admin,path+"/genera",json={"incarichi_ids":[job["incarico_id"]]})
        assert early.status_code==409
        config=_fake_config()
        cfg=admin.put("/api/admin/co-veneto/config",headers={"X-RankUp-Request":"1"},
                      json={"dati":config})
        assert cfg.status_code==200,cfg.text
        assert cfg.json()["mancanti"]==[]
        with server.connect() as db:
            row=db.execute("SELECT source_json FROM candidature WHERE id=?",
                           (job["candidatura_id"],)).fetchone()
            source=json.loads(row["source_json"])
            source["codice_fiscale"]="TEST000000000001"
            db.execute("UPDATE candidature SET codice_fiscale=?,source_json=? WHERE id=?",
                       (source["codice_fiscale"],json.dumps(source),job["candidatura_id"]))
            db.commit()
        view=admin.get(path).json()
        job=next(j for j in view["incarichi"] if j["incarico_id"]==job["incarico_id"])
        work=dict(job["work"])
        work.update({
            "retribuzione":"1234",
            "domicilio_comune_codice":"L781", "nascita_comune_codice":"L781",
            "livello_istruzione":"70", "cittadinanza_codice":"000", "sesso":"F"
        })
        saved=admin.put(path+"/incarichi/"+str(job["incarico_id"]),
                        headers={"X-RankUp-Request":"1"},json={"dati":work})
        assert saved.status_code==200,saved.text
        generated=_post(admin,path+"/genera",json={"incarichi_ids":[job["incarico_id"]]})
        assert generated.status_code==200,generated.text
        approvals=admin.get("/api/bandi/26-02/graduatorie/approvazioni").json()["approvazioni"]
        assert next(a for a in approvals if a["id"]==aid)["co_veneto_zip_stato"]=="parziale"
        batch=admin.get(path+"/lotti/"+str(generated.json()["lotto_id"])+"/zip")
        assert batch.status_code==200
        with ZipFile(BytesIO(batch.content)) as zipfile:
            assert zipfile.testzip() is None
            names=zipfile.namelist()
            assert len(names)==1
            xml=zipfile.read(names[0])
            root=ET.fromstring(xml)
            assert root.attrib["codiceComunicazione"]==("0"*16)
            # Il PDF della ricevuta può mostrare Note conservate dal portale,
            # ma l'XML di origine non contiene un nodo Note.
            assert root.find(".//{"+NS+"}Note") is None
            assert root.find(".//{"+NS+"}note") is None
            assert root.find(".//{"+NS+"}InizioRapporto").attrib["dataFine"]=="2027-09-30"
            assert root.find(".//{"+NS+"}InizioRapporto").attrib["dataInizio"]==job["inizio"]
        with server.connect() as db:
            event=db.execute("""
                SELECT dettagli_json FROM co_veneto_eventi
                WHERE evento='generazione_xml' AND incarico_id=?
                ORDER BY id DESC LIMIT 1
            """, (job["incarico_id"],)).fetchone()
            assert event is not None
            details=json.loads(event["dettagli_json"])
            assert details["note"] == work["note"]
            assert details["note_in_xml"] is False
        last=admin.get(path).json()["incarichi"][0]["ultimo_file"]
        assert last["revisione"]==1
        assert admin.get(path+"/xml/"+str(last["id"])).content==xml

        created=_post(admin,"/api/utenti",json={
            "username":"operatore-co-test","password":"PasswordFittiziaTest2026!"})
        assert created.status_code==200,created.text
        with TestClient(server.app) as user:
            logged=_post(user,"/api/auth/login",json={
                "username":"operatore-co-test","password":"PasswordFittiziaTest2026!"})
            assert logged.status_code==200,logged.text
            _complete_operator_onboarding(user,"operatore-co-test","PasswordFittiziaTest2026!")
            assert user.get(path).status_code==200
            assert user.get("/api/admin/co-veneto/config").status_code==403
            assert user.get(path+"/xml/"+str(last["id"])).content==xml
            blocked=user.put(path+"/incarichi/"+str(job["incarico_id"]),
                             headers={"X-RankUp-Request":"1"},json={"dati":work})
            assert blocked.status_code==403
            assert _post(user,path+"/genera",
                         json={"incarichi_ids":[job["incarico_id"]]}).status_code==403
        assert _post(admin,path+"/genera",
                     json={"incarichi_ids":[job["incarico_id"]]}).status_code==400
        nextgen=_post(admin,path+"/genera",json={
            "incarichi_ids":[job["incarico_id"]],
            "motivazione":"Rigenerazione sintetica di collaudo con traccia"
        })
        assert nextgen.status_code==200,nextgen.text
        assert admin.get(path).json()["incarichi"][0]["ultimo_file"]["revisione"]==2
        assert admin.get(path+"/xml/"+str(last["id"])).content==xml

        # Dopo regressione la creazione è bloccata, ma lo storico resta leggibile.
        regress=_post(admin,"/api/bandi/26-02/graduatorie/approvazioni/"+
                      str(aid)+"/regredisci-stato",
                      json={"motivazione":"Regressione sintetica di collaudo",
                            "riferimento":"RIFERIMENTO SINTETICO VERIFICATO"})
        assert regress.status_code==200,regress.text
        assert admin.get(path).json()["attiva"] is False
        assert _post(admin,path+"/genera",json={
            "incarichi_ids":[job["incarico_id"]]}).status_code==409
        assert admin.get(path+"/xml/"+str(last["id"])).content==xml


def test_pica_residence_city_with_embedded_province_code():
    """La stringa PICA 'VERONA (VR)' contiene una provincia verificabile."""
    from co_veneto_codes import comune_domicilio

    source = {
        "residenza_citta": "VERONA (VR)",
        "residenza_indirizzo": "VIA DI PROVA",
        "residenza_cap": "37142",
    }
    assert comune_domicilio(source) == "L781"
    assert defaults_from_pica(source)["domicilio_comune_codice"] == "L781"

    # Comune e provincia distinti devono produrre lo stesso codice.
    assert comune_domicilio({
        "residenza_citta": "VERONA (VR)", "residenza_provincia": "VR",
    }) == "L781"

    # Fonti PICA discordanti o nazioni estere non vanno risolte per supposizione.
    assert comune_domicilio({
        "residenza_citta": "VERONA (VR)", "residenza_provincia": "PD",
    }) == ""
    assert comune_domicilio({
        "residenza_citta": "VERONA (VR)", "residenza_nazione": "GERMANIA",
    }) == ""
    assert comune_domicilio({"residenza_citta": "VERONA"}) == ""
    assert comune_domicilio({"residenza_citta": "COMUNE INESISTENTE (VR)"}) == ""


def test_select_all_controls_co_veneto_only_for_eligible_records():
    """La selezione massiva non forza i checkbox disabilitati e resta reversibile."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] /
          "rankup/app/static/co_veneto_ui.js").read_text(encoding="utf-8")
    assert 'id="co-select-all"' in js or 'id=\\"co-select-all\\"' in js
    assert '"Deseleziona tutti":"Seleziona tutti"' in js
    assert 'document.querySelectorAll("[data-co-pick]:not(:disabled)")' in js
    assert "pick.checked=selectAll.checked" in js
    assert "selectAll.indeterminate=" in js
    assert "selected+\" di \"+picks.length" in js


def test_notes_auto_from_pica_and_no_empty_separators():
    scope={"cds":"LOGOPEDIA (ABILITANTE ALLA PROFESSIONE SANITARIA DI LOGOPEDISTA)",
           "sede":"VERONA"}
    job={"insegnamento":"LOGOPEDIA GENERALE","modulo":""}
    note=note_from_approval(scope,job,"2026-2027","26-02")
    assert note==(
        "LOGOPEDIA (ABILITANTE ALLA PROFESSIONE SANITARIA DI LOGOPEDISTA)"
        " - VERONA - LOGOPEDIA GENERALE - A.A. 2026/2027; BANDO 26-02"
    )
    assert " - - " not in note
    job["modulo"]="LOGOPEDIA GENERALE"
    assert note_from_approval(scope,job,"2026-2027","26-02")==note
    job["modulo"]="Modulo da PICA"
    assert " - Modulo da PICA - A.A." in note_from_approval(
        scope,job,"2026-2027","26-02")


def test_co_veneto_notes_ui_visible_editable_copy_and_no_xml_injection():
    from pathlib import Path
    script=(Path(__file__).resolve().parents[1] /
            "rankup/app/static/co_veneto_ui.js").read_text(encoding="utf-8")
    assert 'data-co-field="note"' in script
    assert "job.work.note" in script
    assert 'data-co-copy' in script
    assert "navigator.clipboard.writeText" in script
    assert "NON sono trasmesse nell’XML" in script
