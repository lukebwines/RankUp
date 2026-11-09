"""Collaudo della chiusura finale del bando con documenti sintetici."""
from io import BytesIO
import hashlib
import json
from zipfile import ZipFile
from test_pdf_import import _setup, _post
from test_graduatorie import _complete_all


def test_archivio_finale_blocca_incompleti_e_salva_zip(tmp_path, monkeypatch):
    server, client, sheets = _setup(tmp_path, monkeypatch)
    with client:
        base="/api/bandi/26-02"
        assert client.get(base+"/archivio/verifica").json()["pronto"] is False
        assert _post(client,base+"/archivio/chiudi",json={}).status_code==409
        _complete_all(client,sheets)
        info=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        body={"cds":info["cds"],"sede":info["sede"]}
        final=_post(client,base+"/verbali/graduatorie/genera-definitivo",json=body)
        assert final.status_code==200,final.text
        assert _post(client,base+"/archivio/chiudi",json={}).status_code==409
        after=client.get(base+"/verbali/graduatorie").json()["graduatorie"][0]
        draft=_post(client,base+"/graduatorie/approvazioni",
                    json={"graduatorie_ids":[after["conferma"]["id"]]})
        assert draft.status_code==200,draft.text
        approval_id=draft.json()["id"]
        assert _post(client,base+"/archivio/chiudi",json={}).status_code==409
        confirmation=_post(client,base+f"/graduatorie/approvazioni/{approval_id}/conferma",
                           json={"estremi_atto":"PROVVEDIMENTO SINTETICO DI COLLAUDO"})
        assert confirmation.status_code==200,confirmation.text
        assert client.get(base+"/archivio/verifica").json()["pronto"] is True

        completed=_post(client,base+"/archivio/chiudi",json={})
        assert completed.status_code==200,completed.text
        result=completed.json()
        assert result["cds_sedi"]==1
        assert client.get(base).json()["bando"]["stato"]=="Archiviato"
        response=client.get(f"/api/archivio/{result['archivio_id']}/zip")
        assert response.status_code==200,response.text
        assert hashlib.sha256(response.content).hexdigest()==result["sha256"]
        with ZipFile(BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            names=archive.namelist()
            assert "manifest.json" in names
            assert "02_dati/dati_bando.json" in names
            assert "01_originali/pica_originale.xlsx" in names
            assert any(x.startswith("03_verbali_definitivi/") for x in names)
            assert any(x.startswith("04_graduatorie_generali/") for x in names)
            assert any(x.startswith("01_originali/schede_restituite/") for x in names)
            manifest=json.loads(archive.read("manifest.json"))
            assert set(manifest["files"])==set(names)-{"manifest.json"}
            for name,meta in manifest["files"].items():
                assert hashlib.sha256(archive.read(name)).hexdigest()==meta["sha256"]
            exported=json.loads(archive.read("02_dati/dati_bando.json"))
            assert len(exported["candidature"])==3
            assert "rankup_utenti" not in exported
        assert _post(client,base+"/archivio/chiudi",json={}).status_code==409
        assert _post(client,base+"/graduatorie/approvazioni",
                     json={"graduatorie_ids":[after["conferma"]["id"]]}).status_code==409
