"""Vista operativa e diagnostica di RankUP.

Esclusivamente letture: nessun dato di PICA, nomina o graduatoria e' dedotto,
modificato o propagato ad altri bandi. I nominativi dei candidati non escono
da questi report aggregati.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import sqlite3

from commission_match import commission_index, resolve_commission, course_key, seat_key

NO_AA = "__non_associato__"


def _scope(db, anno="", codice=""):
    args=[]
    where=[]
    if anno == NO_AA:
        where.append("COALESCE(r.anno_accademico,'')=''")
    elif anno:
        where.append("r.anno_accademico=?")
        args.append(anno)
    if codice:
        where.append("b.codice=?")
        args.append(codice)
    query="""SELECT b.id,b.codice,b.stato,b.import_at,
                    COALESCE(r.anno_accademico,'') AS anno_accademico,
                    COALESCE(r.data_scadenza,'') AS data_scadenza
             FROM bandi b LEFT JOIN bandi_riferimenti r ON r.bando_id=b.id"""
    if where:
        query+=" WHERE "+" AND ".join(where)
    query+=" ORDER BY b.import_at DESC,b.id DESC"
    return [dict(r) for r in db.execute(query,args)]


def report_operativo(db: sqlite3.Connection, anno="", codice="", username="", status_check=None):
    """Indicatori calcolati dai record presenti, non da stime."""
    bandi=_scope(db,anno,codice)
    if not bandi:
        return {"bandi":[],"attivita":[],"segnalazioni":[],"fasi":[],
                "recenti":[],"scadenze":[],"totale_cds":0}
    ids=[b["id"] for b in bandi]
    bind=",".join("?" for _ in ids)
    places=db.execute(f"""
        SELECT i.bando_id,i.cds,i.sede,COUNT(DISTINCT i.id) AS incarichi,
               COUNT(c.id) AS candidature
        FROM incarichi i LEFT JOIN candidature c
          ON c.incarico_id=i.id AND c.stato='Attiva'
        WHERE i.bando_id IN ({bind})
        GROUP BY i.bando_id,i.cds,i.sede
        ORDER BY i.cds COLLATE NOCASE,i.sede COLLATE NOCASE""",ids).fetchall()
    groups=[dict(r) for r in places]
    rows_comm=db.execute(f"""
        SELECT bando_id,cds,sede,presidente,componenti FROM commissioni
        WHERE bando_id IN ({bind})""",ids).fetchall()
    by_comm=defaultdict(list)
    for r in rows_comm: by_comm[r["bando_id"]].append(dict(r))
    indexes={key:commission_index(rows) for key,rows in by_comm.items()}
    valid=db.execute(f"""
        SELECT i.bando_id,i.cds,i.sede,COUNT(DISTINCT c.id) AS acquisite
        FROM valutazioni_pdf v
        JOIN candidature c ON c.id=v.candidatura_id AND c.stato='Attiva'
        JOIN incarichi i ON i.id=c.incarico_id AND i.bando_id=v.bando_id
        WHERE v.bando_id IN ({bind}) AND v.attiva=1 AND v.stato='Acquisita'
          AND v.decisione IN ('idoneo','non_idoneo','escluso')
        GROUP BY i.bando_id,i.cds,i.sede""",ids).fetchall()
    validated={(r["bando_id"],r["cds"],r["sede"]):r["acquisite"] for r in valid}
    grads=db.execute(f"""
        SELECT g.bando_id,g.cds,g.sede,g.stato,r.graduatoria_id AS riaperta
        FROM graduatorie_cds g LEFT JOIN graduatorie_cds_riaperte r
          ON r.graduatoria_id=g.id WHERE g.bando_id IN ({bind})""",ids).fetchall()
    grad_map={(r["bando_id"],r["cds"],r["sede"]):
              ("" if r["riaperta"] else r["stato"]) for r in grads}
    verbali=db.execute(f"""
        SELECT bando_id,cds,sede FROM verbali_descrittivi
        WHERE attivo=1 AND bando_id IN ({bind})""",ids).fetchall()
    verbali_keys={(r["bando_id"],r["cds"],r["sede"]) for r in verbali}
    tickets=[dict(r) for r in db.execute(f"""
        SELECT s.id,s.bando_id,s.titolo,s.stato,s.assegnato_a,s.creata_il,
               s.commissione_id,s.autore FROM segnalazioni s
        WHERE s.bando_id IN ({bind}) AND s.stato!='risolta'
        ORDER BY s.id DESC""",ids)]
    by_code={b["id"]:b for b in bandi}
    checks={"commissioni":0,"valutazioni":0,"verbali":0,"graduatorie":0}
    tasks=[]
    warnings=[]
    for g in groups:
        b=by_code[g["bando_id"]]
        if b["stato"]=="Archiviato":
            continue
        key=(g["bando_id"],g["cds"],g["sede"])
        cm_status,commission=resolve_commission(
            indexes.get(g["bando_id"],{}),g["cds"],g["sede"])
        has_comm=(cm_status=="associata" and commission
                  and commission.get("presidente") and commission.get("componenti"))
        acquired=int(validated.get(key,0))
        evaluations_ok=(g["candidature"]>0 and acquired==g["candidature"])
        status=None
        if has_comm and evaluations_ok and status_check is not None:
            status=status_check(db,g["bando_id"],g["cds"],g["sede"])
            # I PDF acquisiti possono nascondere parità o esiti non validati.
            evaluations_ok=(status["schede_da_risolvere"]==0)
        has_verbale=key in verbali_keys
        grad=grad_map.get(key,"")
        for field,ok in (("commissioni",has_comm),("valutazioni",evaluations_ok),
                         ("verbali",has_verbale),("graduatorie",bool(grad))):
            checks[field]+=int(bool(ok))
        if not has_comm:
            issue=("Commissione non associata univocamente"
                   if cm_status!="associata" else "Composizione della commissione incompleta")
            area="commissioni";priority=1
        elif not evaluations_ok:
            issue=(str(g["candidature"]-acquired)+" candidature ancora da validare"
                   if g["candidature"] else "Nessuna candidatura attiva da valutare")
            area="valutazioni";priority=2
        elif status is not None and not status["pronta"] and not grad:
            issue="Criticità della graduatoria da verificare"
            area="verbali";priority=2
        elif not has_verbale and not grad:
            issue="Verbale descrittivo non acquisito";area="verbali";priority=2
        elif not grad:
            issue="Verificare la graduatoria CdS";area="verbali";priority=3
        elif grad=="definitiva":
            issue="Graduatoria CdS definitiva: verificare gli atti generali"
            area="graduatorie";priority=3
        else:
            continue
        tasks.append({"bando":b["codice"],"cds":g["cds"],"sede":g["sede"],
                      "problema":issue,"pagina":area,"priorita":priority,
                      "candidature":g["candidature"],"valutazioni_acquisite":acquired,
                      "commissione_associata":bool(has_comm),
                      "verbale_presente":bool(has_verbale),
                      "graduatoria_stato":grad or "da_definire"})
    for b in bandi:
        if b["stato"]=="In preparazione":
            tasks.append({
                "bando":b["codice"],"cds":"","sede":"",
                "problema":"Bando in preparazione: verifica PDF e riferimenti, poi importa PICA",
                "pagina":"incarichi","priorita":3,
                "candidature":0,"valutazioni_acquisite":0,
                "commissione_associata":False,"verbale_presente":False,
                "graduatoria_stato":"da_definire"})
    for ticket in tickets:
        b=by_code[ticket["bando_id"]]
        if b["stato"]=="Archiviato":continue
        warnings.append({
            "id":ticket["id"],"bando":b["codice"],"titolo":ticket["titolo"],
            "stato":ticket["stato"],"assegnato_a":ticket["assegnato_a"],
            "creata_il":ticket["creata_il"],
            "mia":bool(username and ticket["assegnato_a"].casefold()==username.casefold()),
            "pagina":"segnalazioni"
        })
    tasks.sort(key=lambda t:(t["priorita"],t["bando"],t["cds"],t["sede"]))
    all_groups=len([g for g in groups if by_code[g["bando_id"]]["stato"]!="Archiviato"])
    fasi=[{"chiave":k,"titolo":v,"completati":checks[k],"totale":all_groups}
          for k,v in (("commissioni","Commissioni"),("valutazioni","Schede e valutazioni"),
                      ("verbali","Verbali"),("graduatorie","Graduatorie"))]
    deadlines=[{"bando":b["codice"],"data":b["data_scadenza"],
                "tipo":"Scadenza del bando"}
               for b in bandi if b["data_scadenza"]]
    return {"bandi":[{"codice":b["codice"],"stato":b["stato"],
                     "anno_accademico":b["anno_accademico"]} for b in bandi],
            "attivita":tasks,"segnalazioni":warnings,"fasi":fasi,
            "recenti":_recent_activity(db,ids,by_code,limit=12),
            "scadenze":deadlines,"totale_cds":all_groups}


def _recent_activity(db, ids, by_code, limit=30):
    """Unifica le tracce già esistenti evitando testo libero e dati PICA."""
    bind=",".join("?" for _ in ids)
    specs=[
        ("commissioni_eventi","azione","quando","autore","bando_id","Commissioni"),
        ("graduatorie_eventi","evento","quando","autore","bando_id","Graduatorie"),
        ("valutazioni_pdf_eventi","azione","quando","autore","bando_id","Valutazioni"),
        ("bandi_riferimenti_eventi",None,"quando","autore","bando_id","Riferimenti"),
    ]
    events=[]
    available={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table,action,date,author,band,category in specs:
        if table not in available:continue
        action_sql=action if action else "'Aggiornamento'"
        for r in db.execute(f"""SELECT {action_sql} AS azione,{date} AS quando,
                {author} AS autore,{band} AS bando_id FROM {table}
                WHERE {band} IN ({bind}) ORDER BY {date} DESC LIMIT ?""",
                [*ids,limit]):
            events.append({"tipo":category,"azione":r["azione"],
                           "quando":r["quando"],"autore":r["autore"],
                           "bando":by_code[r["bando_id"]]["codice"]})
    if "bandi" in available:
        for r in db.execute(f"""SELECT id,stato,created_at,import_at,source_filename
            FROM bandi WHERE id IN ({bind}) ORDER BY import_at DESC LIMIT ?""",
            [*ids,limit]):
            events.append({"tipo":"Bandi","azione":(
                "Preparazione bando" if r["stato"]=="In preparazione"
                else "Registrazione/importazione PICA"),
                "quando":r["import_at"],"autore":"non registrato",
                "bando":by_code[r["id"]]["codice"]})
    if "segnalazioni" in available:
        for r in db.execute(f"""SELECT bando_id,creata_il,autore FROM segnalazioni
            WHERE bando_id IN ({bind}) ORDER BY creata_il DESC LIMIT ?""",
            [*ids,limit]):
            events.append({"tipo":"Segnalazioni","azione":"Nuova segnalazione",
                           "quando":r["creata_il"],"autore":r["autore"],
                           "bando":by_code[r["bando_id"]]["codice"]})
    return sorted(events,key=lambda x:x["quando"],reverse=True)[:limit]


def report_diagnostico(db: sqlite3.Connection, backups_dir=None):
    """Diagnostica sola lettura. Non modifica ne' ripara alcun record."""
    tables={r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    results={
        "versione_schema":db.execute("PRAGMA user_version").fetchone()[0],
        "integrita":db.execute("PRAGMA integrity_check").fetchone()[0],
        "violazioni_fk":[{"tabella":r[0],"riga":r[1],"referenza":r[2]}
              for r in db.execute("PRAGMA foreign_key_check")],
        "controlli":[],
        "divergenze_commissioni":[],
        "backup_schema":[],
    }
    def check(name,required,query):
        if not set(required)<=tables:
            results["controlli"].append({"nome":name,"esito":"non_applicabile"})
            return
        n=int(db.execute(query).fetchone()[0])
        results["controlli"].append({"nome":name,"totale":n,
                                    "esito":"ok" if n==0 else "attenzione"})
    check("Candidature collegate a incarichi di altro bando",("candidature","incarichi"),
          """SELECT COUNT(*) FROM candidature c JOIN incarichi i ON i.id=c.incarico_id
             WHERE c.bando_id<>i.bando_id""")
    check("Segnalazioni collegate a commissioni di altro bando",
          ("segnalazioni","commissioni"),
          """SELECT COUNT(*) FROM segnalazioni s JOIN commissioni c ON c.id=s.commissione_id
             WHERE s.bando_id<>c.bando_id""")
    check("Graduatorie senza CdS e sede negli incarichi",
          ("graduatorie_cds","incarichi"),
          """SELECT COUNT(*) FROM graduatorie_cds g WHERE NOT EXISTS (
             SELECT 1 FROM incarichi i WHERE i.bando_id=g.bando_id
             AND i.cds=g.cds AND i.sede=g.sede)""")
    check("Candidature duplicate nello stesso incarico e bando",("candidature",),
          """SELECT COUNT(*) FROM (SELECT bando_id,incarico_id,id_pica FROM candidature
             GROUP BY 1,2,3 HAVING COUNT(*)>1)""")
    check("Rettifiche senza commissione in anagrafica",
          ("commissioni_catalogo_rettifiche","commissioni_catalogo"),
          """SELECT COUNT(*) FROM commissioni_catalogo_rettifiche r
             LEFT JOIN commissioni_catalogo c ON c.id=r.catalogo_id WHERE c.id IS NULL""")
    if {"commissioni","bandi"}<=tables:
        seen=set()
        collisions=0
        for row in db.execute("SELECT bando_id,cds,sede FROM commissioni"):
            key=(row["bando_id"],course_key(row["cds"]),seat_key(row["sede"]))
            if key in seen:collisions+=1
            seen.add(key)
        results["controlli"].append({
            "nome":"Commissioni con CdS/sede equivalenti nello stesso bando",
            "totale":collisions,"esito":"attenzione" if collisions else "ok"})
    if {"commissioni_catalogo","commissioni","bandi"}<=tables:
        all_catalog=[dict(r) for r in db.execute(
            """SELECT id,cds,sede,presidente,componenti,anno
               FROM commissioni_catalogo ORDER BY id DESC""")]
        current={}
        for item in all_catalog:
            current.setdefault((course_key(item["cds"]),seat_key(item["sede"])),item)
        for r in db.execute("""SELECT c.id,c.bando_id,c.cds,c.sede,c.presidente,c.componenti,
                                      b.codice FROM commissioni c
                               JOIN bandi b ON b.id=c.bando_id"""):
            ref=current.get((course_key(r["cds"]),seat_key(r["sede"])))
            if ref and (ref["presidente"]!=r["presidente"] or
                        ref["componenti"]!=r["componenti"]):
                results["divergenze_commissioni"].append({
                    "bando":r["codice"],"cds":r["cds"],"sede":r["sede"],
                    "commissione_id":r["id"],"catalogo_id":ref["id"],
                    "nota":"Composizione del bando diversa dalla più recente in anagrafica. Verifica documentale necessaria."
                })
    if backups_dir is not None:
        try:
            folder=backups_dir
            if folder.is_dir():
                found=[p for p in folder.glob("*.sqlite3") if p.is_file()]
                for p in sorted(found,key=lambda v:v.stat().st_mtime,reverse=True)[:5]:
                    results["backup_schema"].append({
                        "file":p.name,"dimensione_byte":p.stat().st_size,
                        "aggiornato_il":datetime.fromtimestamp(
                            p.stat().st_mtime,timezone.utc).isoformat()})
        except OSError:
            results["backup_verifica"]="Impossibile leggere la cartella backup"
    results["esito"] = ("attenzione" if results["integrita"]!="ok" or
                        results["violazioni_fk"] or
                        any(c["esito"]=="attenzione" for c in results["controlli"])
                        else "ok")
    return results

def global_registry_activity(db: sqlite3.Connection, limit=40):
    """Eventi dell'anagrafica non attribuibili a uno specifico bando."""
    tables={r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    events=[]
    if "commissioni_catalogo_eventi" in tables:
        for r in db.execute("""SELECT azione,autore,quando
              FROM commissioni_catalogo_eventi ORDER BY id DESC LIMIT ?""",(limit,)):
            events.append({"tipo":"Anagrafica commissioni","azione":r["azione"],
                           "quando":r["quando"],"autore":r["autore"],
                           "bando":"Anagrafica"})
    if "commissioni_catalogo_rettifiche" in tables:
        for r in db.execute("""SELECT autore,quando
              FROM commissioni_catalogo_rettifiche ORDER BY id DESC LIMIT ?""",(limit,)):
            events.append({"tipo":"Anagrafica commissioni",
                           "azione":"Rettifica errore materiale",
                           "quando":r["quando"],"autore":r["autore"],
                           "bando":"Anagrafica"})
    return sorted(events,key=lambda x:x["quando"],reverse=True)[:limit]
