"""Regressioni V3: vista operativa e pre-importazioni sicure, dati sintetici."""
from io import BytesIO
import json
from openpyxl import load_workbook
from fastapi.testclient import TestClient
from test_import import pica_fixture
from test_pdf_import import _setup, _post


def _pica_with_code(code):
    workbook=load_workbook(BytesIO(pica_fixture()))
    workbook.active["A1"]="univr "+code+" - Esportazione dati"
    output=BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_dashboard_operativo_and_admin_diagnostics_are_read_only(tmp_path, monkeypatch):
    server, admin, _=_setup(tmp_path, monkeypatch)
    with admin:
        with server.connect() as db:
            before=db.execute("SELECT COUNT(*) FROM candidature").fetchone()[0]
        response=admin.get("/api/dashboard/operativo?anno_accademico=__non_associato__")
        assert response.status_code==200,response.text
        data=response.json()
        assert "attivita" in data and "fasi" in data
        assert any(t["bando"]=="26-02" for t in data["attivita"])
        assert all("cognome" not in json.dumps(t) for t in data["attivita"])
        detail=admin.get("/api/bandi/26-02/verifiche-cds")
        assert detail.status_code==200,detail.text
        assert detail.json()["verifiche"]
        diag=admin.get("/api/admin/diagnostica")
        assert diag.status_code==200,diag.text
        assert diag.json()["integrita"]=="ok"
        assert not diag.json()["violazioni_fk"]
        events=admin.get("/api/admin/registro-attivita")
        assert events.status_code==200,events.text
        with server.connect() as db:
            after=db.execute("SELECT COUNT(*) FROM candidature").fetchone()[0]
        assert before==after


def test_operator_can_read_operational_dashboard_but_not_diagnostics(tmp_path, monkeypatch):
    server, admin, _=_setup(tmp_path, monkeypatch)
    with admin:
        created=_post(admin,"/api/utenti",json={
            "username":"operatore-ops","password":"PasswordOperatore123!"})
        assert created.status_code==200,created.text
        with TestClient(server.app) as operator:
            logged=_post(operator,"/api/auth/login",json={
                "username":"operatore-ops","password":"PasswordOperatore123!"})
            assert logged.status_code==200
            assert operator.get("/api/dashboard/operativo").status_code==200
            assert operator.get("/api/bandi/26-02/verifiche-cds").status_code==200
            assert operator.get("/api/admin/diagnostica").status_code==403
            assert operator.get("/api/admin/registro-attivita").status_code==403


def test_pica_other_code_requires_explicit_admin_confirmation(tmp_path, monkeypatch):
    server, admin, _=_setup(tmp_path, monkeypatch)
    with admin:
        prepared=_post(admin,"/api/bandi",json={
            "codice":"26-03","anno_accademico":"2026-2027"})
        assert prepared.status_code==200,prepared.text
        sample=_pica_with_code("26-04")
        preview=_post(admin,"/api/pica/preview",files={
            "file":("pica_sintetico.xlsx",sample,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"anno_accademico":"2026-2027"})
        assert preview.status_code==200,preview.text
        info=preview.json()
        assert info["codice_bando"]=="26-04"
        assert "26-03" in info["bandi_preparati_diversi"]
        denied=_post(admin,"/api/pica/commit",json={
            "token":info["token"],"anno_accademico":"2026-2027"})
        assert denied.status_code==409,denied.text
        with server.connect() as db:
            assert db.execute(
                "SELECT id FROM bandi WHERE codice='26-04'").fetchone() is None
        imported=_post(admin,"/api/pica/commit",json={
            "token":info["token"],"anno_accademico":"2026-2027",
            "conferma_bando_distinto":True})
        assert imported.status_code==200,imported.text
        assert imported.json()["codice_bando"]=="26-04"
        with server.connect() as db:
            event=db.execute("""SELECT autore,codice_pica FROM pica_import_eventi
                WHERE bando_id=(SELECT id FROM bandi WHERE codice='26-04')""").fetchone()
            assert event and event["autore"]=="test-admin"
            assert event["codice_pica"]=="26-04"
        assert next(x for x in admin.get("/api/bandi").json()["bandi"]
                    if x["codice"]=="26-03")["candidature"]==0
        repeated=_post(admin,"/api/pica/preview",files={
            "file":("file_rinominato.xlsx",sample,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"anno_accademico":"2026-2027"})
        assert repeated.status_code==409


def test_file_sha_duplicate_across_codes_is_blocked(tmp_path, monkeypatch):
    server, admin, _=_setup(tmp_path, monkeypatch)
    with admin:
        import hashlib
        data=_pica_with_code("26-04")
        digest=hashlib.sha256(data).hexdigest()
        with server.connect() as db:
            # Scenario sintetico: stesso file associato erroneamente ad altro codice.
            db.execute("""INSERT INTO bandi
                (codice,stato,created_at,source_filename,source_hash,import_at)
                VALUES('26-77','Candidature importate','test','test',?,'test')""",(digest,))
        preview=_post(admin,"/api/pica/preview",files={
            "file":("originale.xlsx",data,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert preview.status_code==409
        assert "identico" in preview.json()["detail"].lower()


def test_operational_ui_contains_filters_navigation_and_real_data_endpoint():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/
          "rankup/app/static/index.html").read_text(encoding="utf-8")
    for marker in ("Attività che richiedono attenzione","ru-ops-filters",
                   "renderOperationalTasks","renderOperationalPhases",
                   "renderDiagnostica","dashboard/operativo?",
                   "data-page=\"diagnostica\"","conferma_bando_distinto",
                   "operativo.css"):
        assert marker in html


def test_exact_prepared_code_import_does_not_require_extra_confirmation(tmp_path, monkeypatch):
    server, admin, _=_setup(tmp_path,monkeypatch)
    with admin:
        for code in ("26-03","26-04"):
            assert _post(admin,"/api/bandi",json={
                "codice":code,"anno_accademico":"2026-2027"}).status_code==200
        sample=_pica_with_code("26-04")
        preview=_post(admin,"/api/pica/preview",files={
            "file":("candidature.xlsx",sample,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"anno_accademico":"2026-2027"})
        assert preview.status_code==200,preview.text
        assert preview.json()["bandi_preparati_diversi"]==[]
        imported=_post(admin,"/api/pica/commit",json={
            "token":preview.json()["token"],"anno_accademico":"2026-2027"})
        assert imported.status_code==200,imported.text


def test_cross_code_identifier_overlap_warning_not_automatic_cancellation(tmp_path, monkeypatch):
    server, admin, _=_setup(tmp_path,monkeypatch)
    with admin:
        sample=_pica_with_code("26-04")
        preview=_post(admin,"/api/pica/preview",files={
            "file":("candidature.xlsx",sample,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert preview.status_code==200,preview.text
        overlaps=preview.json()["possibili_duplicati"]
        assert overlaps and overlaps[0]["codice"]=="26-02"
        assert overlaps[0]["id_pica_in_comune"]>0
        assert all("cognome" not in json.dumps(o) for o in overlaps)
