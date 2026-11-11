"""Regressioni v1.0.14 - dati esclusivamente sintetici e chiamate AI simulate."""
import json
from io import BytesIO

from pypdf import PdfReader
from ai_groq import DEFAULT_GUIDANCE, REVISED_GUIDANCE, safe_institution_name
from test_ai_queue_v109 import _prepare
from test_pdf_import import _post

HEADERS={"X-RankUp-Request":"1"}


def test_institutional_guidance_and_privacy_reduction():
    assert "AZIENDA ZERO VENETO" in REVISED_GUIDANCE
    assert "ASST" in REVISED_GUIDANCE
    assert "AZIENDA ZERO VENETO" not in DEFAULT_GUIDANCE
    assert safe_institution_name(
        "AULSS 9 Scaligera - REPARTO DI PEDIATRIA")=="AULSS 9 SCALIGERA"
    assert safe_institution_name(
        "AOUI Verona - DIPARTIMENTO DI EMERGENZA")=="AOUI VERONA"
    assert safe_institution_name(
        "AULSS 9 Scaligera - REPARTO Sig. Rossi"
    )=="DENOMINAZIONE NON TRASMETTIBILE"
    assert safe_institution_name(
        "AULSS 9 Scaligera - REPARTO 3331234567"
    )=="DENOMINAZIONE NON TRASMETTIBILE"
    assert safe_institution_name(
        "AOUI Verona \n c/o sig. Rossi"
    )=="DENOMINAZIONE NON TRASMETTIBILE"


def _complete_job(server,sender):
    for _ in range(12):
        if not server.ai_process_one(server.connect,server._ai_worker_candidates,
                                     sender,server.GROQ.key,server.GROQ.limits()):
            return
    raise AssertionError("Coda non conclusa dopo 12 iterazioni sintetiche")


def test_selected_reanalysis_does_not_spread_to_unselected_candidates(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        first=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert first.status_code==200,first.text
        calls=[]
        def coherent(key,model,payload,guidance=None):
            assert set(payload)=={"requisito_ente","denominazione_ente_dichiarato"}
            calls.append(dict(payload))
            return {"indizio":"INDIZI COERENTI","motivo":"Ente istituzionale noto",
                    "verifiche":"Verificare rapporto di lavoro", "origine":"groqcloud",
                    "token_usati":70}
        _complete_job(server,coherent)
        assert len(calls)==1
        original=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()
        assert original["conteggi"]["ENTE COERENTE"]==3
        ids=[r["id"] for r in original["casi"]]
        selected=ids[0]
        targeted=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True,
            "ids_candidature":[selected]})
        assert targeted.status_code==200,targeted.text
        assert targeted.json()["totale_candidature"]==1
        assert targeted.json()["selezione_mirata"] is True
        def insufficient(*args,**kwargs):
            return {"indizio":"DATI INSUFFICIENTI",
                    "motivo":"Dichiarazione ambigua",
                    "verifiche":"Controllare denominazione ente", "origine":"groqcloud",
                    "token_usati":83}
        _complete_job(server,insufficient)
        updated=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()
        by_id={r["id"]:r for r in updated["casi"]}
        assert updated["conteggi"]["ENTE COERENTE"]==2
        assert updated["conteggi"]["DA APPROFONDIRE"]==1
        assert by_id[selected]["tipo_approfondimento"]=="DATI INSUFFICIENTI"
        assert all(by_id[i]["esito"]=="ENTE COERENTE" for i in ids[1:])
        assert updated["conteggi_tipo"]["DATI INSUFFICIENTI"]==1
        # Cronologia completa: i risultati precedenti non sono cancellati.
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM ai_ente_ai_esiti").fetchone()[0]==2
            targeted_flag=db.execute("SELECT mirato FROM ai_ente_lavori WHERE id=?",
                 (targeted.json()["lavoro"]["id"],)).fetchone()[0]
            assert targeted_flag==1
        filtered=client.get("/api/admin/ai/verifiche",
            params={"code":"26-02","tipo":"DATI INSUFFICIENTI"})
        assert filtered.status_code==200,filtered.text
        assert filtered.json()["visualizzati"]==1
        pdf=client.get("/api/admin/ai/report.pdf",
            params={"code":"26-02","esito":"DA APPROFONDIRE"})
        assert pdf.status_code==200,pdf.text
        summary="\n".join(page.extract_text() or ""
                          for page in PdfReader(BytesIO(pdf.content)).pages)
        assert "DATI INSUFFICIENTI" in summary
        assert "Tipo di approfondimento:" in summary
        # La cache generica continua a riferirsi al risultato non mirato.
        again=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True})
        assert again.status_code==200,again.text
        stat=client.get("/api/admin/ai/lavori",params={"code":"26-02"}).json()["lavoro"]
        assert stat["riutilizzati"]==3
        after=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()
        assert after["conteggi"]["ENTE COERENTE"]==2
        assert after["conteggi"]["DA APPROFONDIRE"]==1


def test_rejects_selective_analysis_of_non_transmittable_fields(tmp_path,monkeypatch):
    server,client=_prepare(tmp_path,monkeypatch)
    with client:
        selected=client.get("/api/admin/ai/verifiche",params={"code":"26-02"}).json()["casi"][0]["id"]
        with server.connect() as db:
            record=db.execute("SELECT source_json FROM candidature WHERE id=?",
                              (selected,)).fetchone()
            original=json.loads(record["source_json"])
            original["azienda_ente"]="AULSS 9 - REPARTO SIG. ROSSI"
            db.execute("UPDATE candidature SET source_json=? WHERE id=?",
                       (json.dumps(original),selected))
            db.commit()
        rejected=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True,
            "ids_candidature":[selected]})
        assert rejected.status_code==400,rejected.text
        assert "non inviabile" in rejected.text
        duplicate=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True,
            "ids_candidature":[selected,selected]})
        assert duplicate.status_code==400,duplicate.text
        unknown=_post(client,"/api/admin/ai/lavori",json={
            "code":"26-02","conferma_servizio":True,
            "ids_candidature":[999999999]})
        assert unknown.status_code==400,unknown.text


def test_realistic_institution_names_with_punctuation_are_not_privacy_blocked():
    """Solo denominazioni di fantasia o generiche: nessun dato candidato."""
    names=[
        "ASUIT: Azienda Sanitaria Universitaria Integrata del Trentino",
        "ASUIT - Azienda Sanitaria Universitaria Integrata del Trentino",
        "AULSS8 BERICA, DISTRETTO OVEST",
        'Azienda U.L.S.S. 3 "Serenissima"',
        "ULSS 3 Serenissima - Ospedale SS. Giovanni e Paolo, Venezia",
        'IRCCS - Ospedale Sacro Cuore Don Calabria',
        'Ospedale IRCCS "Sacro Cuore - Don Calabria"',
        "AULSS 5 Polesana - Ospedale San Luca, Trecenta",
        'M.I.M. - Liceo Statale "G. Cotta"',
        "AOU Padova",
        "Natus Medical",
        "BCC ravennate, forlivese e imolese",
    ]
    from ai_groq import prepare_minimal_payload
    from ai_ente_rules import classifica_ente
    for name in names:
        normalized=safe_institution_name(name)
        assert normalized not in ("NON DICHIARATO",
                "DENOMINAZIONE NON TRASMETTIBILE"), name
        result=classifica_ente({
            "ente_richiesto":"SSN","azienda_ente":name,
            "occupazione":"Dipendente",
        })
        assert result["richiede_ai"],name
        assert result["tipo_approfondimento"]=="IN ATTESA AI",name
        assert prepare_minimal_payload({
            "ente_richiesto":"SSN","azienda_ente":name
        })["denominazione_ente_dichiarato"]==normalized


def test_candidate_identifiers_still_block_transmission():
    from ai_groq import prepare_minimal_payload
    candidate={
        "nome":"PersonaTest","cognome":"CognomeTest",
        "ente_richiesto":"SSN",
    }
    protected=[
        "AZIENDA SANITARIA, contatto: persona@example.test",
        "AULSS 9; tel. +39 333 123 4567",
        "AOUI - Sig. Mario Rossi",
        "AULSS - Dott. Mario Rossi",
        "AULSS 9 - REPARTO SIG. ROSSI",
        "ASST\nNome personale",
        "<script>alert(1)</script>",
        "AOUI Verona, PersonaTest",
        "AOU Padova - CognomeTest",
        "AULSS 9 Codice Fiscale RSSMRA80A01H501U",
        "AOU Padova, via Roma 2",
        "MARIO ROSSI",
    ]
    for name in protected:
        payload=prepare_minimal_payload({**candidate,"azienda_ente":name})
        assert payload["denominazione_ente_dichiarato"]==(
            "DENOMINAZIONE NON TRASMETTIBILE"),name
    # L'invio all'AI non deve mai contenere nomi, PICA, qualifiche o recapiti.
    business=prepare_minimal_payload({
        **candidate,"azienda_ente":"Natus Medical",
        "id_pica":"SYNTH-0001",
        "occupazione":"Dipendente: +39 333 123 4567",
    })
    assert set(business)=={
        "denominazione_ente_dichiarato","requisito_ente",
        "ateneo_dichiarato","occupazione_dichiarata",
        "profilo_professionale_dichiarato","qualifica_dichiarata",
    }
    assert business["occupazione_dichiarata"]=="DENOMINAZIONE NON TRASMETTIBILE"
    assert business["denominazione_ente_dichiarato"]=="NATUS MEDICAL"
    assert "PersonaTest" not in str(business)
    assert "SYNTH-0001" not in str(business)
