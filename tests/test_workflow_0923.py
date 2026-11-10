"""Regressioni sintetiche per le decisioni funzionali RankUP 0.9.23."""
import hashlib
from io import BytesIO

from pypdf import PdfReader
from test_pdf_import import _setup, _post, _edit, _preview, _confirm
from test_graduatorie import _complete_all


def _approve(client):
    base="/api/bandi/26-02"
    group=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
    place={"cds":group["cds"],"sede":group["sede"]}
    final=_post(client,base+"/verbali/graduatorie/genera-definitivo",json=place)
    assert final.status_code==200,final.text
    gid=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]["conferma"]["id"]
    draft=_post(client,base+"/graduatorie/approvazioni",json={"graduatorie_ids":[gid]})
    assert draft.status_code==200,draft.text
    aid=draft.json()["id"]
    approved=_post(client,base+f"/graduatorie/approvazioni/{aid}/conferma",
                   json={"estremi_atto":"ATTO SINTETICO DI COLLAUDO"})
    assert approved.status_code==200,approved.text
    return group,place,aid


def test_rinuncia_admin_non_richiede_scheda_e_traccia_evento(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        base="/api/bandi/26-02"
        cand=client.get(base+"/candidature").json()["candidature"][0]
        bad=_post(client,base+f"/candidature/{cand['id']}/stato",json={
            "stato":"Rinunciata","motivazione":"Comunicazione formale di rinuncia",
            "riferimento":"[DA VERIFICARE]"})
        assert bad.status_code==400
        ok=_post(client,base+f"/candidature/{cand['id']}/stato",json={
            "stato":"Rinunciata","motivazione":"Rinuncia documentata al singolo incarico",
            "riferimento":"COMUNICAZIONE SINTETICA"})
        assert ok.status_code==200,ok.text
        assert ok.json()["scheda_obbligatoria"] is False
        group=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        assert group["totale_candidature"]==2
        assert client.get(base+"/valutazioni").json()["totale_attese"]==2
        events=client.get(base+f"/candidature/{cand['id']}/eventi").json()["eventi"]
        assert len(events)==1 and events[0]["successivo"]=="Rinunciata"
        assert _post(client,base+f"/candidature/{cand['id']}/stato",json={
            "stato":"Rinunciata","motivazione":"Rinuncia documentata al singolo incarico",
            "riferimento":"COMUNICAZIONE SINTETICA"}).status_code==409


def test_tutte_ritirate_verbale_senza_schede_fittizie(tmp_path,monkeypatch):
    _,client,_=_setup(tmp_path,monkeypatch)
    with client:
        base="/api/bandi/26-02"
        for cand in client.get(base+"/candidature").json()["candidature"]:
            response=_post(client,base+f"/candidature/{cand['id']}/stato",json={
                "stato":"Ritirata","motivazione":"Ritiro comunicato dalla candidatura",
                "riferimento":"COMUNICAZIONE SINTETICA"})
            assert response.status_code==200,response.text
        group=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        assert group["totale_candidature"]==0
        assert group["totale_annullate"]==3
        assert group["verbale_generabile"] is True
        final=_post(client,base+"/verbali/graduatorie/genera-definitivo",
                    json={"cds":group["cds"],"sede":group["sede"]})
        assert final.status_code==200,final.text


def test_parita_documentata_non_altera_punteggi(tmp_path,monkeypatch):
    _,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        base="/api/bandi/26-02"
        docs=[(f"scheda_{i}.pdf",_edit(pdf,{
            "P01":"2","TOT":"2","STATUS":"CANDIDATO AMMESSO IN GRADUATORIA"}))
            for i,pdf in enumerate(sheets.values())]
        data=_preview(client,docs)
        assert _confirm(client,data["token"])["acquisite"]==3
        group=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        job=next(j for j in group["incarichi"] if j.get("parita"))
        tie=job["parita"][0]
        pending={c["candidatura_id"] for c in tie["candidati"]}
        assert len(pending)==2 and not group["pronta"]
        report=client.get(base+"/valutazioni").json()["archivio"]
        assert {c["candidatura_id"] for c in report if c["verifica_parita"]}==pending
        selected=[c["candidatura_id"] for c in reversed(tie["candidati"])]
        response=_post(client,base+f"/verbali/graduatorie/{job['id']}/parita",json={
            "punteggio":tie["punteggio"],"ordine_candidature":selected,
            "motivazione":"Ordine stabilito dalla commissione",
            "riferimento":"VERBALE SINTETICO COMMISSIONE"})
        assert response.status_code==200,response.text
        group=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        assert group["verbale_generabile"] and group["pronta"]
        resolved=next(j for j in group["incarichi"] if j["id"]==job["id"])
        ordered=sorted((c for c in resolved["candidature"] if c["candidatura_id"] in pending),
                       key=lambda c:c["posizione"])
        assert [c["candidatura_id"] for c in ordered]==selected
        assert len({c["punteggio"] for c in ordered})==1
        assert not any(c["verifica_parita"] for c in client.get(base+"/valutazioni").json()["archivio"])


def test_prospetto_riservato_e_scorrimenti_fuori_app(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        base="/api/bandi/26-02"
        _complete_all(client,sheets)
        group,_,aid=_approve(client)
        public=client.get(base+f"/graduatorie/approvazioni/{aid}/pdf")
        internal=client.get(base+f"/graduatorie/approvazioni/{aid}/prospetto-interno")
        assert public.status_code==200 and internal.status_code==200
        text="\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(internal.content)).pages)
        assert "USO INTERNO" in text
        assert all(c["id_pica"] in text for j in group["incarichi"] for c in j["candidature"])
        assert internal.content!=public.content
        with server.connect() as db:
            audit=db.execute("""SELECT COUNT(*) FROM graduatorie_eventi
                       WHERE evento='Download prospetto nominativo interno'""").fetchone()[0]
            assert audit==1
        cand=client.get(base+"/candidature").json()["candidature"][0]
        denied=_post(client,base+f"/candidature/{cand['id']}/stato",json={
            "stato":"Rinunciata","motivazione":"Rinuncia dopo approvazione",
            "riferimento":"COMUNICAZIONE SINTETICA"})
        assert denied.status_code==409


def test_chiusura_rifiuta_pdf_assente_e_recupero_tracciato(tmp_path,monkeypatch):
    server,client,sheets=_setup(tmp_path,monkeypatch)
    with client:
        base="/api/bandi/26-02"
        _complete_all(client,sheets)
        _,place,_=_approve(client)
        original=client.get(base+"/verbali/graduatorie/pdf-completo",params=place).content
        with server.connect() as db:
            db.execute("DELETE FROM graduatorie_cds_documenti")
            db.commit()
        check=client.get(base+"/archivio/verifica").json()
        assert not check["pronto"] and any("PDF definitivo" in x for x in check["criticita"])
        assert _post(client,base+"/archivio/chiudi",json={}).status_code==409
        assert client.get(base+"/verbali/graduatorie/pdf-completo",params=place).status_code==409
        fields={"cds":place["cds"],"sede":place["sede"],
                "motivazione":"Recupero verbale realmente adottato",
                "riferimento":"VERBALE SINTETICO COMMISSIONE"}
        missing=_post(client,base+"/verbali/graduatorie/recupera-pdf",
                      data=fields,files={"file":("storico.pdf",original,"application/pdf")})
        assert missing.status_code==400
        fields["conferma_corrispondenza"]="true"
        ok=_post(client,base+"/verbali/graduatorie/recupera-pdf",
                 data=fields,files={"file":("storico.pdf",original,"application/pdf")})
        assert ok.status_code==200,ok.text
        assert ok.json()["sha256"]==hashlib.sha256(original).hexdigest()
        assert _post(client,base+"/verbali/graduatorie/recupera-pdf",
              data=fields,files={"file":("storico.pdf",original,"application/pdf")}).status_code==409
        assert client.get(base+"/archivio/verifica").json()["pronto"]
        result=_post(client,base+"/archivio/chiudi",json={})
        assert result.status_code==200,result.text
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM graduatorie_cds_documenti_recuperi").fetchone()[0]==1
