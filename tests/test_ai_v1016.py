"""Collaudo 1.0.16: contesto PICA completo, test esclusivamente sintetici."""
import json

from ai_groq import (prepare_minimal_payload, safe_context_text,
                     has_institutional_context, ENT_E_PRIVATE, ENT_E_MISSING)
from ai_ente_rules import classifica_ente
from test_import import make_excel
from pica import parse_pica


def test_pica_extra_columns_are_read_by_exact_headers_not_by_position():
    header=["Campo sintetico "+str(i) for i in range(1,69)]
    for i,name in {
        1:"ID domanda",2:"Stato",3:"Corso di Studio",4:"sede",
        5:"Nome insegnamento padre",6:"Descrizione Copertura",
        7:"ID Copertura",8:"Cognome",9:"Nome",53:"Profilo Professionale",
        54:"Azienda/ente",56:"Ateneo",65:"Occupazione",67:"Qualifica",
    }.items():
        header[i-1]=name
    row=[""]*len(header)
    for i,val in {
        1:90001,2:"presentata",3:"CdS sintetico",4:"Verona",
        5:"Modulo demo",6:"Copertura demo",7:100,8:"Fittizio",9:"Esempio",
        53:"Docente",54:"",56:"Universita' di fantasia",
        65:"Ricercatore",67:"Professore associato",
    }.items():
        row[i-1]=val
    result=parse_pica("synthetic.xlsx",make_excel([
        ["univr 26-02 - Esportazione dati"],header,row,
    ]))
    record=result["rows"][0]
    assert record["azienda_ente"]==""
    assert record["ateneo"]=="Universita' di fantasia"
    assert record["occupazione"]=="Ricercatore"
    assert record["profilo_professionale"]=="Docente"
    assert record["qualifica"]=="Professore associato"
    # Anche spostando le colonne l'import segue l'intestazione.
    header[55],header[66]=header[66],header[55]
    row[55],row[66]=row[66],row[55]
    second=parse_pica("synthetic.xlsx",make_excel([
        ["univr 26-02 - Esportazione dati"],header,row,
    ]))["rows"][0]
    assert second["ateneo"]=="Universita' di fantasia"
    assert second["qualifica"]=="Professore associato"


def test_without_bb_the_ai_gets_ateneo_occupation_profile_and_qualification():
    item={
        "ente_richiesto":"ALTRO ATENEO", "azienda_ente":"",
        "ateneo":"Universita' di fantasia",
        "occupazione":"Ricercatore presso Universita' di fantasia",
        "profilo_professionale":"Docenza universitaria",
        "qualifica":"Professore associato",
        "nome":"Esempio", "cognome":"Fittizio",
        "id_pica":"SYNTH-0001",
        "email":"fittizio@example.test",
    }
    payload=prepare_minimal_payload(item)
    assert payload=={
        "requisito_ente":"ALTRO ATENEO",
        "denominazione_ente_dichiarato":"NON DICHIARATO",
        "ateneo_dichiarato":"UNIVERSITA' DI FANTASIA",
        "occupazione_dichiarata":"RICERCATORE PRESSO UNIVERSITA' DI FANTASIA",
        "profilo_professionale_dichiarato":"DOCENZA UNIVERSITARIA",
        "qualifica_dichiarata":"PROFESSORE ASSOCIATO",
    }
    assert "SYNTH-0001" not in str(payload)
    assert "fittizio@example.test" not in str(payload)
    row=classifica_ente(item)
    assert row["richiede_ai"] is True
    assert row["tipo_approfondimento"]=="IN ATTESA AI"
    assert "Ateneo" in row["fonti_pica"]
    assert "Qualifica" in row["fonti_pica"]


def test_occupation_can_name_institution_even_with_bb_empty():
    item={
        "ente_richiesto":"SSN","azienda_ente":"","ateneo":"",
        "occupazione":"Dirigente sanitario presso AOUI Verona",
        "profilo_professionale":"Dirigente medico",
        "qualifica":"Dirigente sanitario",
    }
    data=prepare_minimal_payload(item)
    assert data["denominazione_ente_dichiarato"]==ENT_E_MISSING
    assert data["occupazione_dichiarata"]=="DIRIGENTE SANITARIO PRESSO AOUI VERONA"
    assert has_institutional_context(data)
    assert classifica_ente(item)["richiede_ai"]


def test_profile_qualification_alone_do_not_identify_employer():
    row=classifica_ente({
        "ente_richiesto":"SSN","azienda_ente":"","ateneo":"",
        "occupazione":"","profilo_professionale":"Dipendente",
        "qualifica":"Dirigente sanitario",
    })
    assert row["richiede_ai"] is False
    assert row["tipo_approfondimento"]=="ENTE NON DICHIARATO"


def test_sensitive_occupation_omitted_but_safe_ateneo_retained():
    data=prepare_minimal_payload({
        "ente_richiesto":"ALTRO ATENEO","azienda_ente":"",
        "ateneo":"Universita' di fantasia",
        "occupazione":"Ricercatore contatto: persona@example.test",
        "profilo_professionale":"Docente",
        "qualifica":"Ricercatore",
        "nome":"Persona", "cognome":"Test",
        "id_pica":"TEST-12345",
    })
    assert data["occupazione_dichiarata"]==ENT_E_PRIVATE
    assert data["ateneo_dichiarato"]=="UNIVERSITA' DI FANTASIA"
    assert has_institutional_context(data)
    assert "persona@example.test" not in json.dumps(data)
    assert "TEST-12345" not in json.dumps(data)
    assert safe_context_text("Prof. Mario Rossi")==ENT_E_PRIVATE


def test_legacy_cache_visible_as_historical_without_new_context_claim(tmp_path,monkeypatch):
    from ai_ente_queue import _hash,LEGACY_RULES_VERSION
    from test_ai_queue_v109 import _prepare
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        with server.connect() as db:
            bid,candidates=server._ai_scope(db,"26-02","","")
            first=candidates[0]
            inc=db.execute("SELECT id FROM incarichi WHERE bando_id=? AND key=?",
                (bid,first["incarico_key"])).fetchone()
            old={"requisito_ente":"SSN",
                 "denominazione_ente_dichiarato":"AOUI VERONA"}
            old_hash=_hash(old,server.GROQ.model(),server.GROQ.guidance(),
                           LEGACY_RULES_VERSION)
            db.execute("""INSERT INTO ai_ente_ai_esiti
                (bando_id,candidatura_id,incarico_id,input_sha256,modello,
                 versione_regole,indizio,motivo,verifiche,token,registrato_il)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (bid,first["id"],inc["id"],old_hash,server.GROQ.model(),
                 LEGACY_RULES_VERSION,"INDIZI COERENTI",
                 "Valutazione sintetica precedente",
                 "Controllare documentazione",0,"2026-01-01T00:00:00"))
            db.commit()
        report=client.get("/api/admin/ai/verifiche",params={"code":"26-02"})
        assert report.status_code==200,report.text
        rows=report.json()["casi"]
        assert all(r["esito"]=="ENTE COERENTE" for r in rows)
        assert all(r.get("valutazione_storica") for r in rows)
        assert all("NON esaminati" in r["verifiche"] for r in rows)
