"""GroqCloud: solo confronto preliminare sull'ente, senza dati candidato.

Il prompt remoto contiene soltanto codici di requisito, categoria istituzionale
dichiarata e qualifica astratta. ID PICA e candidati restano in RankUP.
I giudizi della AI non modificano ammissibilità, punteggi o atti.
"""
from __future__ import annotations
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib import error, request
import hashlib
import json
import os
import re
import secrets

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from xml.sax.saxutils import escape

API_CHAT = "https://api.groq.com/openai/v1/chat/completions"
API_MODELS = "https://api.groq.com/openai/v1/models"
_ALLOWED_RESULTS = {"INDIZI COERENTI", "POSSIBILE DISALLINEAMENTO", "DATI INSUFFICIENTI"}
_ALLOWED_REQUISITES = {"AOUI", "ASUIT", "SSN", "UNITN", "APSS", "SSP",
                       "ALTRO ATENEO", "ASUIT O UNITN", "NESSUNA RISERVA"}
MAX_RESULT_LENGTH = 440
MODEL_RE = re.compile(r"^[a-zA-Z0-9_.:/-]{2,125}$")
SYSTEM_PROMPT = (
    "Sei un assistente amministrativo. Valuta esclusivamente l'eventuale "
    "corrispondenza tra requisito di ente e CATEGORIA di ente dichiarato; "
    "non è accertata alcuna assunzione o condizione contrattuale. "
    "I dati sono semplici categorie, non documenti probatori. "
    "Restituisci SOLO JSON con indizio in "
    "['INDIZI COERENTI','POSSIBILE DISALLINEAMENTO','DATI INSUFFICIENTI'], "
    "motivo e verifiche, entrambi stringhe in italiano. "
    "Non dedurre idoneità, non inventare fatti o riferimenti, "
    "non suggerire automatismi decisionali. In caso di dubbio scegli "
    "'DATI INSUFFICIENTI'."
)


def _normal(value: object) -> str:
    return " ".join(str(value or "").upper().split())


def ente_category(text: str) -> str:
    """Riduce una denominazione libera a una categoria: niente testo inviato."""
    label=_normal(text)
    if not label:
        return "NON DICHIARATO"
    if any(x in label for x in ("AOUI VERONA","AZIENDA OSPEDALIERA UNIVERSITARIA INTEGRATA DI VERONA",
                                "AZIENDA OSPEDALIERA UNIVERSITARIA INTEGRATA VERONA")):
        return "AOUI"
    if any(x in label for x in ("ASUIT","AZIENDA SANITARIA UNIVERSITARIA INTEGRATA DEL TRENTINO")):
        return "ASUIT"
    if any(x in label for x in ("APSS","AZIENDA PROVINCIALE PER I SERVIZI SANITARI")):
        return "APSS"
    if any(x in label for x in ("SABES","AZIENDA SANITARIA DELL'ALTO ADIGE",
                                "SUDTIROLER SANITATSBETRIEB")):
        return "SSP"
    if any(x in label for x in ("UNIVERSITÀ DI TRENTO","UNIVERSITA DI TRENTO",
                                "UNIVERSITA DEGLI STUDI DI TRENTO", "UNITN")):
        return "UNITN"
    if any(x in label for x in ("UNIVERSITA", "UNIVERSITÀ", "ATENEO", "POLITECNICO")):
        return "ALTRO ATENEO"
    if any(x in label for x in ("AZIENDA ULSS", "AZIENDA USL", "A.S.L.",
                                " ASL ", "AZIENDA SANITARIA LOCALE",
                                "AZIENDA OSPEDALIERA", "AZIENDA SANITARIA")):
        return "STRUTTURA SANITARIA (SSN DA VERIFICARE)"
    return "ENTE NON CLASSIFICATO"


def role_category(occupation: str, profile: str) -> str:
    line=_normal(occupation+" "+profile)
    if not line:
        return "NON DICHIARATO"
    if any(x in line for x in ("PROFESSORE", "PROFESSORESSA", "RICERCATORE", "RICERCATRICE")):
        return "DOCENZA O RICERCA"
    if any(x in line for x in ("LIBERO PROFESSIONISTA", "LIBERA PROFESSIONISTA",
                               "AUTONOMO", "AUTONOMA", "PARTITA IVA")):
        return "LAVORO AUTONOMO"
    if any(x in line for x in ("DIPENDENTE", "ASSUNTO", "ASSUNTA", "DIRIGENTE",
                               "CONTRATTO A TEMPO INDETERMINATO",
                               "CONTRATTO A TEMPO DETERMINATO")):
        return "RAPPORTO DIPENDENTE DICHIARATO"
    return "QUALIFICA NON CLASSIFICATA"


def prepare_minimal_payload(entry: dict) -> dict:
    """Whitelisting: ID PICA e ogni campo libero non attraversano la rete."""
    req=_normal(entry.get("ente_richiesto"))
    return {
        "requisito_ente": req if req in _ALLOWED_REQUISITES else "NON CLASSIFICATO",
        "categoria_ente_dichiarato": ente_category(entry.get("azienda_ente","")),
        "categoria_rapporto_dichiarato": role_category(
            str(entry.get("occupazione") or ""), str(entry.get("profilo_professionale") or "")),
    }


def source_hash(payload: dict, model: str) -> str:
    return hashlib.sha256(json.dumps(
        [payload,model],sort_keys=True,ensure_ascii=False
    ).encode("utf-8")).hexdigest()


def _fetch_json(url: str, key: str, payload: dict | None=None) -> dict:
    headers={"Authorization":"Bearer "+key,"Accept":"application/json"}
    data=None
    if payload is not None:
        data=json.dumps(payload,ensure_ascii=False).encode("utf-8")
        headers["Content-Type"]="application/json"
    req=request.Request(url,data=data,headers=headers,
                        method="POST" if payload is not None else "GET")
    # Destinazione fissa HTTPS: non seguire redirect verso altre origini.
    class NoRedirect(request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):
            return None
    try:
        with request.build_opener(NoRedirect()).open(req,timeout=25) as resp:
            data=resp.read(400_001)
        if len(data)>400_000:
            raise ValueError("Risposta troppo grande")
        doc=json.loads(data)
        if not isinstance(doc,dict):
            raise ValueError("Formato non valido")
        return doc
    except error.HTTPError as exc:
        if exc.code==429:
            try: pause=int(float(exc.headers.get("Retry-After","60")))
            except ValueError: pause=60
            raise GroqLimit(max(10,min(pause,86400))) from None
        if exc.code in (401,403):
            raise ValueError("Chiave GroqCloud non autorizzata") from None
        if exc.code==404:
            raise ValueError("Modello GroqCloud non disponibile") from None
        raise ValueError("GroqCloud non disponibile o richiesta non accettata") from None
    except (error.URLError,OSError,TimeoutError):
        raise ValueError("Connessione GroqCloud non disponibile") from None


class GroqLimit(Exception):
    def __init__(self, retry_after: int):
        self.retry_after=retry_after
        super().__init__("Limite GroqCloud raggiunto")


class GroqSecrets:
    def __init__(self,root: Path):
        self.root=root
        self.file=root/".groqcloud_key"
        self.model_file=root/"groqcloud_model.json"
    def key(self) -> str:
        try:
            return self.file.read_text("utf-8").strip()
        except OSError:
            return ""
    def model(self) -> str:
        try:
            data=json.loads(self.model_file.read_text("utf-8"))
            return data.get("model","") if MODEL_RE.fullmatch(data.get("model","")) else ""
        except (OSError,ValueError,TypeError):
            return ""
    def save(self,key: str|None,model: str) -> None:
        if model and not MODEL_RE.fullmatch(model):
            raise ValueError("Identificativo modello non valido")
        if key is not None and key:
            if len(key)<12 or len(key)>256 or any(c.isspace() for c in key):
                raise ValueError("Chiave API non valida")
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if key:
            _atomic_secret(self.file,key)
        elif key=="":
            self.file.unlink(missing_ok=True)
        _atomic_secret(self.model_file,json.dumps({"model":model}))


def _atomic_secret(path:Path,value:str)->None:
    filename=path.parent/("."+path.name+"."+secrets.token_hex(8))
    fd=os.open(str(filename),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        os.replace(filename,path)
        path.chmod(0o600)
    finally:
        filename.unlink(missing_ok=True)


def models(key:str) -> list[str]:
    if not key:
        raise ValueError("Inserire prima la chiave GroqCloud")
    info=_fetch_json(API_MODELS,key)
    data=info.get("data")
    if not isinstance(data,list):
        raise ValueError("Elenco modelli non disponibile")
    return sorted({x["id"] for x in data
                   if isinstance(x,dict) and isinstance(x.get("id"),str)
                   and MODEL_RE.fullmatch(x["id"])})[:200]


def analyze(key:str,model:str,payload:dict) -> dict:
    """Non accetta stringhe PICA complete; solo categorie whitelist."""
    expected={"requisito_ente","categoria_ente_dichiarato","categoria_rapporto_dichiarato"}
    if set(payload)!=expected:
        raise ValueError("Campi del quesito non consentiti")
    if not key or not MODEL_RE.fullmatch(model):
        raise ValueError("Configurazione GroqCloud incompleta")
    # Nessuna chiamata per casi senza fonte significativa.
    if (payload["requisito_ente"] in ("NESSUNA RISERVA","NON CLASSIFICATO")
        or payload["categoria_ente_dichiarato"] in
           ("NON DICHIARATO","ENTE NON CLASSIFICATO")):
        return {"indizio":"DATI INSUFFICIENTI",
                "motivo":"Non esistono categorie verificate sufficienti per l'analisi.",
                "verifiche":"Verificare manualmente la dichiarazione e il requisito ufficiale."}
    res=_fetch_json(API_CHAT,key,{
        "model":model,
        "messages":[
            {"role":"system","content":SYSTEM_PROMPT},
            {"role":"user","content":json.dumps(payload,ensure_ascii=False)},
        ],
        "temperature":0.1,"max_tokens":350,
    })
    try:
        txt=res["choices"][0]["message"]["content"].strip()
        txt=re.sub(r"^"+r"'''" +r"(?:json)?\s*|"+r"\s*'''" +r"$","",txt)
        txt=re.sub(r"^```(?:json)?\s*|\s*```$", "",txt)
        obj=json.loads(txt)
        if not isinstance(obj,dict) or obj.get("indizio") not in _ALLOWED_RESULTS:
            raise ValueError("Esito non valido")
        if not isinstance(obj.get("motivo"),str) or not obj["motivo"].strip():
            raise ValueError("Motivazione mancante")
        if not isinstance(obj.get("verifiche"),str) or not obj["verifiche"].strip():
            raise ValueError("Verifiche mancanti")
        return {"indizio":obj["indizio"],
                "motivo":obj["motivo"][:MAX_RESULT_LENGTH].strip(),
                "verifiche":obj["verifiche"][:MAX_RESULT_LENGTH].strip()}
    except (KeyError,IndexError,TypeError,ValueError):
        raise ValueError("Risposta AI non utilizzabile, nessuna conclusione registrata") from None


def report_pdf(code:str,cds:str,sede:str,rows:list[dict]) -> bytes:
    """Il PDF contiene ID PICA locali, non nomi né CF né testi grezzi PICA."""
    bio=BytesIO()
    doc=SimpleDocTemplate(bio,pagesize=A4,leftMargin=48,rightMargin=48,
        topMargin=54,bottomMargin=50,title="Controllo preliminare ente "+code)
    base=ParagraphStyle("normal",fontName="Helvetica",fontSize=9,
                        leading=13,spaceAfter=6,alignment=TA_LEFT)
    heading=ParagraphStyle("head",parent=base,fontName="Helvetica-Bold",
                           fontSize=13,leading=17,spaceAfter=14)
    small=ParagraphStyle("small",parent=base,fontSize=8,leading=11)
    def p(value,style=base):
        return Paragraph(escape(str(value)),style)
    story=[p("RANKUP - Verifica preliminare del requisito di ente",heading),
           p("Bando: "+code+" | CdS: "+cds+" | Sede: "+sede),
           p("Analisi di supporto, non costituisce verifica dei rapporti di lavoro, "
             "giudizio di idoneita', ammissione o decisione della Commissione.",small),
           Spacer(1,8)]
    for item in rows:
        pair=item["documento"]
        story.append(KeepTogether([
            p("ID PICA: "+str(pair["id_pica"])+" | Incarico: "+pair["insegnamento"],base),
            p("Requisito: "+item["requisito"]+" | Ente dichiarato (categoria): "+
              item["categoria_ente"]),
            p("Esito istruttorio: "+item["indizio"]),
            p("Motivo: "+item["motivo"]),
            p("Da verificare: "+item["verifiche"]),
            Spacer(1,9)]))
    if not rows:
        story.append(p("Nessun risultato disponibile per l'ambito selezionato."))
    doc.build(story)
    return bio.getvalue()
