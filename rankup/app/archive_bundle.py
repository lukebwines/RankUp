"""Archiviazione finale di un bando, solo dopo tutte le approvazioni.

Nessun dato amministrativo è inventato: si esportano solo record e documenti
presenti sul sistema. ZIP locale, storico immutabile e manifest di integrità.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from zipfile import ZipFile, ZIP_DEFLATED

from pypdf import PdfReader
from io import BytesIO

class ArchivioNonPronto(ValueError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def controlla_chiusura(db, bando_id: int) -> dict:
    """Controlla TUTTI i CdS/sedi con incarichi PICA, non solo i CdS con PDF."""
    locations=db.execute("""
        SELECT cds,sede,COUNT(*) AS incarichi FROM incarichi
        WHERE bando_id=? GROUP BY cds,sede
        ORDER BY cds COLLATE NOCASE,sede COLLATE NOCASE
    """,(bando_id,)).fetchall()
    issues=[]
    if not locations:
        issues.append("Il bando non contiene incarichi PICA.")
    results=[]
    for place in locations:
        row=db.execute("""
            SELECT g.id,g.stato,g.approvazione_id,g.dati_json,
                   d.sha256 AS hash_pdf,d.pdf_definitivo,
                   r.graduatoria_id AS riaperta,
                   a.stato AS stato_atto,a.estremi_atto,
                   ar.approvazione_id AS atto_regredito
            FROM graduatorie_cds g
            LEFT JOIN graduatorie_cds_documenti d ON d.graduatoria_id=g.id
            LEFT JOIN graduatorie_cds_riaperte r ON r.graduatoria_id=g.id
            LEFT JOIN graduatorie_approvazioni a ON a.id=g.approvazione_id
            LEFT JOIN graduatorie_approvazioni_regressioni ar
              ON ar.approvazione_id=a.id
            WHERE g.bando_id=? AND g.cds=? AND g.sede=?
        """,(bando_id,place["cds"],place["sede"])).fetchone()
        if not row or row["stato"]!="approvata" or row["riaperta"] or not row["approvazione_id"]:
            issues.append(place["cds"]+" / "+place["sede"]+": verbale e approvazione non completati.")
            continue
        if (row["stato_atto"]!="approvata" or row["atto_regredito"]
                or not (row["estremi_atto"] or "").strip()
                or "[DA VERIFICARE]" in row["estremi_atto"].upper()):
            issues.append(place["cds"]+" / "+place["sede"]+": atto di approvazione mancante o regredito.")
            continue
        approved=json.loads(row["dati_json"])
        if approved.get("cds")!=place["cds"] or approved.get("sede")!=place["sede"]:
            issues.append(place["cds"]+" / "+place["sede"]+": istantanea non corrispondente.")
            continue
        results.append({"id":row["id"],"cds":place["cds"],"sede":place["sede"],
                        "approvazione_id":row["approvazione_id"],
                        "pdf_salvato":row["pdf_definitivo"] is not None})
    drafts=db.execute("""SELECT COUNT(*) FROM graduatorie_approvazioni
                         WHERE bando_id=? AND stato='bozza'""",(bando_id,)).fetchone()[0]
    if drafts:
        issues.append("Sono presenti "+str(drafts)+" bozze di approvazione non concluse.")
    approvals=db.execute("""SELECT COUNT(DISTINCT approvazione_id)
                            FROM graduatorie_cds
                            WHERE bando_id=? AND stato='approvata'""",(bando_id,)).fetchone()[0]
    if not approvals:
        issues.append("Nessuna approvazione generale completata.")
    return {"pronto":not issues,"criticita":issues,
            "cds_sedi_totali":len(locations),"cds_sedi_approvati":len(results),
            "approvazioni_completate":approvals,"bozze_aperte":drafts,
            "ambiti":results}


def crea_pacchetto(db, bando: dict, source_root: Path, temporary_zip: Path,
                   pdf_cds, pdf_generale) -> dict:
    """Crea ZIP da snapshot SQLite autonomo, senza bloccare le scritture operative.

    pdf_cds(cds,sede) e pdf_generale(approvazione) devono leggere dati reali,
    senza creare nuovi atti, senza modificare le fonti.
    """
    code=bando["codice"]
    status=controlla_chiusura(db,bando["id"])
    if not status["pronto"]:
        raise ArchivioNonPronto("Chiusura non possibile: "+"; ".join(status["criticita"]))
    if bando["stato"]=="Archiviato":
        raise ArchivioNonPronto("Bando già archiviato.")

    manifest_files={}
    def write_stream(zf, path, file_handle):
        if path in manifest_files:
            raise ArchivioNonPronto("Percorso duplicato nel pacchetto: "+path)
        checksum=hashlib.sha256()
        size=0
        with zf.open(path,"w",force_zip64=True) as dest:
            while True:
                chunk=file_handle.read(1024*1024)
                if not chunk:break
                checksum.update(chunk)
                size+=len(chunk)
                dest.write(chunk)
        meta={"sha256":checksum.hexdigest(),"bytes":size}
        manifest_files[path]=meta
        return meta

    def write_bytes(zf, path, data):
        if path in manifest_files:
            raise ArchivioNonPronto("Percorso duplicato nel pacchetto: "+path)
        if not isinstance(data,bytes):
            raise ArchivioNonPronto("Contenuto archivistico non valido: "+path)
        zf.writestr(path,data)
        manifest_files[path]={"sha256":_sha(data),"bytes":len(data)}

    def safe_name(label):
        return re.sub(r"[^A-Za-z0-9_.-]+","_",str(label)).strip("._")[:75] or "senza_nome"

    with ZipFile(temporary_zip,"w",ZIP_DEFLATED,compresslevel=6,allowZip64=True) as zf:
        # Originali di bando, PICA e ogni versione PDF ricevuta, conservati senza alterazione.
        original_folder=(source_root/code)
        if not original_folder.is_dir():
            raise ArchivioNonPronto("La cartella degli originali PICA/PDF non è disponibile.")
        originals={}
        for f in sorted(original_folder.rglob("*")):
            if f.is_symlink():
                raise ArchivioNonPronto("Collegamento simbolico negli originali: "+f.name)
            if not f.is_file():
                continue
            if not f.resolve().is_relative_to(original_folder.resolve()):
                raise ArchivioNonPronto("Originale fuori dalla cartella del bando.")
            relative=f.relative_to(original_folder).as_posix()
            with f.open("rb") as file_handle:
                originals[relative]=write_stream(zf,"01_originali/"+relative,file_handle)
        if not originals:
            raise ArchivioNonPronto("Nessun documento originale conservato.")
        # Il PICA deve essere verificabile anche come documento originale.
        pica=[meta for name,meta in originals.items()
              if name in ("pica_originale.xls","pica_originale.xlsx")]
        if bando["source_hash"]:
            if len(pica)!=1 or pica[0]["sha256"]!=bando["source_hash"]:
                raise ArchivioNonPronto("Originale PICA mancante o impronta non corrispondente.")
        # Tutte le valutazioni archiviate devono avere un originale presente e integro.
        for item in db.execute("""SELECT id,percorso,sha256 FROM valutazioni_pdf
                                  WHERE bando_id=?""",(bando["id"],)).fetchall():
            path=(source_root.parent/item["percorso"]).resolve()
            if not path.is_relative_to(original_folder.resolve()) or not path.is_file():
                raise ArchivioNonPronto("Originale della scheda "+str(item["id"])+" mancante.")
            original=path.relative_to(original_folder.resolve()).as_posix()
            meta=originals.get(original)
            if not meta or meta["sha256"]!=item["sha256"]:
                raise ArchivioNonPronto("Impronta originale scheda "+str(item["id"])+" non corrispondente.")

        # Snapshot di tutte le tabelle operative appartenenti al bando,
        # senza includere credenziali o anagrafiche di altri bandi.
        export={}
        export["bandi"]=[dict(bando)]
        tables=db.execute("""
            SELECT name FROM sqlite_master WHERE type='table'
              AND name NOT LIKE 'sqlite_%' ORDER BY name
        """).fetchall()
        for tbl in tables:
            table=tbl["name"]
            if table in ("bandi","bandi_archivi"):
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",table):
                continue
            columns={r["name"] for r in db.execute('PRAGMA table_info("'+table+'")')}
            if "bando_id" not in columns:
                continue
            rows=db.execute('SELECT * FROM "'+table+'" WHERE bando_id=?',
                            (bando["id"],))
            exported=[]
            for index,row in enumerate(rows):
                values={}
                for col in row.keys():
                    val=row[col]
                    if isinstance(val,bytes):
                        binary_path="08_database_blob/"+table+"/"+str(index+1)+"_"+safe_name(col)+".bin"
                        write_bytes(zf,binary_path,val)
                        values[col]={"blob_path":binary_path,"sha256":_sha(val),"bytes":len(val)}
                    else:
                        values[col]=val
                exported.append(values)
            export[table]=exported

        # Configurazioni globali pertinenti al bando, mai utenti/password.
        ssd=db.execute("""SELECT s.* FROM ssd_classificazioni s
                          WHERE s.ssd IN (
                             SELECT DISTINCT UPPER(TRIM(ssd))
                             FROM incarichi WHERE bando_id=?
                          ) ORDER BY s.ssd""",(bando["id"],)).fetchall()
        export["ssd_classificazioni_rilevanti"]=[dict(row) for row in ssd]
        export["pica_tipo_cds_dizionario"]=[
            dict(row) for row in db.execute("SELECT * FROM pica_tipo_cds_dizionario ORDER BY chiave")
        ]
        write_bytes(zf,"02_dati/dati_bando.json",
                    json.dumps(export,ensure_ascii=False,sort_keys=True,indent=2).encode("utf-8"))

        # Verbali CdS definitivi con schede e versioni storiche.
        for i,place in enumerate(status["ambiti"],1):
            raw=pdf_cds(place["cds"],place["sede"])
            reader=PdfReader(BytesIO(raw),strict=False)
            if not raw.startswith(b"%PDF-") or len(reader.pages)<1:
                raise ArchivioNonPronto("PDF del CdS non valido: "+place["cds"])
            destination=("03_verbali_definitivi/"+str(i).zfill(3)+"_"+
                         safe_name(place["cds"])+"_"+safe_name(place["sede"])+".pdf")
            write_bytes(zf,destination,raw)

        # Ciascuna approvazione attualmente valida, con gli estremi registrati.
        approved_ids=sorted({x["approvazione_id"] for x in status["ambiti"]})
        for approval_id in approved_ids:
            row=db.execute("""SELECT * FROM graduatorie_approvazioni
                              WHERE id=? AND bando_id=?""",
                           (approval_id,bando["id"])).fetchone()
            if not row or row["stato"]!="approvata":
                raise ArchivioNonPronto("Approvazione generale non verificabile.")
            raw=pdf_generale(dict(row))
            if not raw.startswith(b"%PDF-") or len(PdfReader(BytesIO(raw)).pages)<1:
                raise ArchivioNonPronto("PDF graduatoria generale non valido.")
            write_bytes(zf,"04_graduatorie_generali/approvazione_"+
                        str(approval_id)+".pdf",raw)

        # Riepilogo in formato interoperabile, senza creare dati di atto.
        riepilogo={
            "codice_bando":code,
            "cds_sedi":status["cds_sedi_totali"],
            "approvazioni":len(approved_ids),
            "generato_il":datetime.now(timezone.utc).isoformat(),
            "contenuto":"PICA originale, schede e documenti acquisiti, verbali CdS definitivi, "
                        "graduatorie generali, versioni storiche e dati di audit."
        }
        write_bytes(zf,"00_riepilogo.json",
                    json.dumps(riepilogo,ensure_ascii=False,indent=2).encode("utf-8"))
        manifest={
            "formato":"RankUP-archivio-bando-v1",
            "codice_bando":code,
            "data_creazione":riepilogo["generato_il"],
            "ambiti":status["cds_sedi_totali"],
            "approvazioni":len(approved_ids),
            "files":manifest_files,
            "avvertenza":"L'archivio contiene dati personali: conservarlo in modo protetto. "
                         "Non sostituisce firma digitale, protocollo o conservazione a norma."
        }
        zf.writestr("manifest.json",json.dumps(manifest,ensure_ascii=False,
                                               indent=2,sort_keys=True).encode("utf-8"))

    with ZipFile(temporary_zip,"r") as test:
        expected=set(manifest_files)|{"manifest.json"}
        if set(test.namelist())!=expected:
            raise ArchivioNonPronto("Indice ZIP incompleto.")
        # Leggere i membri a blocchi e confrontare SHA-256; ZipFile controlla
        # anche il CRC senza duplicare la decompressione con testzip().
        for member,expected_meta in manifest_files.items():
            digest=hashlib.sha256()
            size=0
            with test.open(member,"r") as stream:
                while True:
                    chunk=stream.read(1024*1024)
                    if not chunk:break
                    digest.update(chunk)
                    size+=len(chunk)
            if digest.hexdigest()!=expected_meta["sha256"] or size!=expected_meta["bytes"]:
                raise ArchivioNonPronto("Verifica impronta fallita: "+member)
    with temporary_zip.open("rb") as stream:
        archive_hash=hashlib.file_digest(stream,"sha256").hexdigest()
    return {"manifest":manifest,"riepilogo":riepilogo,"file_count":len(manifest_files),
            "sha256":archive_hash,"bytes":temporary_zip.stat().st_size}
