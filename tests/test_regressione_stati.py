"""Regressione controllata e conservazione degli atti RankUP."""
from io import BytesIO

from pypdf import PdfReader
from test_pdf_import import _setup, _post
from test_graduatorie import _complete_all


def _finalize(client, sheets):
    _complete_all(client, sheets)
    base = "/api/bandi/26-02"
    ready = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]
    body = {"cds": ready["cds"], "sede": ready["sede"]}
    assert ready["verbale_generabile"], ready["criticita"]
    final = _post(client, base + "/verbali/graduatorie/definitiva", json=body)
    assert final.status_code == 200, final.text
    return base, body


def test_cds_definitiva_riapertura_archivio_e_nuova_finalizzazione(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        base, body = _finalize(client, sheets)
        prior = client.get(base + "/verbali/graduatorie/pdf-completo", params=body)
        assert prior.status_code == 200
        assert len(PdfReader(BytesIO(prior.content)).pages) > 1

        response = _post(client, base + "/verbali/graduatorie/regredisci-stato",
            json={**body, "motivazione": "Riapertura istruttoria documentata"})
        assert response.status_code == 200, response.text
        revision = response.json()["revisione_id"]
        reopened = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]
        assert reopened["stato"] == "pronta"
        assert reopened["conferma"] is None
        assert reopened["revisioni"][0]["id"] == revision
        archived = client.get(base + f"/verbali/graduatorie/revisioni/{revision}/pdf")
        assert archived.status_code == 200
        assert archived.headers["content-type"] == "application/pdf"
        assert archived.content == prior.content

        assert _post(client, base + "/verbali/graduatorie/regredisci-stato",
            json={**body, "motivazione": "Seconda riapertura non ammessa"}).status_code == 409
        assert _post(client, base + "/verbali/graduatorie/definitiva",
            json=body).status_code == 200
        finalized = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]
        assert finalized["stato"] == "definitiva"
        assert finalized["revisioni"][0]["id"] == revision
        assert client.get(base + f"/verbali/graduatorie/revisioni/{revision}/pdf").content == prior.content
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM graduatorie_cds_revisioni").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM graduatorie_cds_riaperte").fetchone()[0] == 0


def test_approvazione_formale_regressione_e_blocco_cds(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        base, body = _finalize(client, sheets)
        gid = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]["conferma"]["id"]
        draft = _post(client, base + "/graduatorie/approvazioni",
                      json={"graduatorie_ids": [gid]})
        assert draft.status_code == 200, draft.text
        approval_id = draft.json()["id"]
        cds_regress = base + "/verbali/graduatorie/regredisci-stato"
        assert _post(client, cds_regress,
            json={**body, "motivazione": "Riapertura con bozza in corso"}).status_code == 409

        approved = _post(client, base + f"/graduatorie/approvazioni/{approval_id}/conferma",
                         json={"estremi_atto": "ATTO SINTETICO DI COLLAUDO"})
        assert approved.status_code == 200, approved.text
        original = client.get(base + f"/graduatorie/approvazioni/{approval_id}/pdf")
        assert original.status_code == 200
        assert _post(client, cds_regress,
            json={**body, "motivazione": "Riapertura senza regressione generale"}).status_code == 409

        general_regress = base + f"/graduatorie/approvazioni/{approval_id}/regredisci-stato"
        missing_reference = _post(client, general_regress,
            json={"motivazione": "Annullamento formale della precedente approvazione"})
        assert missing_reference.status_code == 400
        reverted = _post(client, general_regress,
            json={"motivazione": "Annullamento formale della precedente approvazione",
                  "riferimento": "PROVVEDIMENTO SINTETICO DI COLLAUDO"})
        assert reverted.status_code == 200, reverted.text
        assert reverted.json()["graduatorie_riaperte"] == 1

        approvals = client.get(base + "/graduatorie/approvazioni").json()["approvazioni"]
        assert approvals[0]["stato"] == "regredita"
        assert approvals[0]["regressione_riferimento"] == "PROVVEDIMENTO SINTETICO DI COLLAUDO"
        assert client.get(base + f"/graduatorie/approvazioni/{approval_id}/pdf").content == original.content
        cds = client.get(base + "/verbali/graduatorie").json()["graduatorie"][0]
        assert cds["stato"] == "definitiva"
        assert _post(client, general_regress,
            json={"motivazione": "Non effettuare una seconda regressione",
                  "riferimento": "PROVVEDIMENTO SINTETICO DI COLLAUDO"}).status_code == 409

        reopened = _post(client, cds_regress,
            json={**body, "motivazione": "Riapertura dopo regressione dell'approvazione"})
        assert reopened.status_code == 200, reopened.text
        with server.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM graduatorie_approvazioni_regressioni").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM graduatorie_cds_revisioni").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM graduatorie_eventi WHERE evento LIKE 'Regressione%'").fetchone()[0] >= 2
