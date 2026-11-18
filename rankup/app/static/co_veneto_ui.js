/* CO Veneto: interfaccia di configurazione e generazione, senza trasmissione. */
(function(){
"use strict";
window.createCoVenetoUI=function(ctx){
  var state=ctx.state,api=ctx.api,html=ctx.html,panel=ctx.panel,
      esc=ctx.esc,go=ctx.go,note=ctx.note;
  var groups=[
    ["Datore di lavoro",[
      ["codice_fiscale","Codice fiscale datore"],
      ["denominazione","Denominazione"],["settore_ateco","Codice ATECO"],
      ["cls_settore","Classificazione settore"],
      ["pubblica_amministrazione","Pubblica amministrazione (SI/NO)"]
    ]],
    ["Rappresentante legale",[
      ["legale_cognome","Cognome"],["legale_nome","Nome"],
      ["legale_sesso","Sesso (M/F)"],["legale_cittadinanza","Codice cittadinanza"],
      ["legale_comune_nascita","Codice catastale comune di nascita"],
      ["legale_data_nascita","Data di nascita (GG/MM/AAAA)"],
      ["legale_soggiornante","Soggiornante in Italia (SI/NO)"]
    ]],
    ["Sede legale",[
      ["sede_legale_comune","Codice catastale comune"],["sede_legale_cap","CAP"],
      ["sede_legale_indirizzo","Indirizzo"],["sede_legale_telefono","Telefono"],
      ["sede_legale_fax","Fax"],["sede_legale_email","E-mail"]
    ]],
    ["Sede di lavoro",[
      ["sede_lavoro_comune","Codice catastale comune"],["sede_lavoro_cap","CAP"],
      ["sede_lavoro_indirizzo","Indirizzo"],["sede_lavoro_telefono","Telefono"],
      ["sede_lavoro_fax","Fax"],["sede_lavoro_email","E-mail"]
    ]],
    ["Codifiche UNILAV",[
      ["email_delegato","E-mail del soggetto che comunica"],
      ["ente_previdenziale","Codice ente previdenziale"],
      ["pat_inail","PAT INAIL"],["qualifica_professionale","Qualifica professionale ISTAT"],
      ["ccnl","Codice CCNL"],["livello_inquadramento","Livello inquadramento"],
      ["tipo_orario","Tipo orario"]
    ]]
  ];
  var contractFields=[
    ["retribuzione","Retribuzione annua lorda (€)"],
    ["domicilio_indirizzo","Indirizzo di domicilio"],
    ["domicilio_citta","Comune domicilio (descrizione)"],
    ["domicilio_cap","CAP domicilio"],
    ["domicilio_comune_codice","Codice catastale comune domicilio"],
    ["nascita_comune_codice","Codice catastale comune nascita"],
    ["cittadinanza_codice","Codice cittadinanza"],
    ["livello_istruzione","Codice livello istruzione"],
    ["sesso","Sesso (M/F)"]
  ];
  function inputFor(key,label,value,locked){
    return '<label style="display:block;min-width:0"><span class="caption">'+esc(label)+'</span>'+
      '<input type="text" data-co-field="'+esc(key)+'" value="'+esc(value||"")+'"'+
      (locked?' readonly':'')+' style="display:block;width:100%;margin-top:5px"></label>';
  }
  function collect(container){
    var values={};
    container.querySelectorAll("[data-co-field]").forEach(function(input){
      values[input.dataset.coField]=input.value.trim();
    });
    return values;
  }
  function url(code,id){
    return "bandi/"+encodeURIComponent(code)+"/co-veneto/approvazioni/"+encodeURIComponent(id);
  }
  async function renderAdmin(){
    html(panel("CO Veneto · Ateneo","Caricamento configurazione..."));
    try{
      var response=await api("admin/co-veneto/config");
      if(state.page!=="coadmin")return;
      var sections=groups.map(function(group){
        return '<div class="card mb"><h3 class="section-heading">'+esc(group[0])+'</h3>'+
          '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px">'+
          group[1].map(function(field){
            return inputFor(field[0],field[1],response.dati[field[0]],false);
          }).join("")+'</div></div>';
      }).join("");
      html('<div class="notice mb"><strong>Configurazione globale · Solo Admin.</strong> '+
        'I dati della comunicazione di esempio non vengono copiati automaticamente. '+
        'Inserire soltanto dati dell’Ateneo verificati; la tipologia B.03.00 resta fissa.</div>'+
        '<form id="co-admin-form">'+sections+
        '<button class="btn" type="submit">Salva configurazione CO Veneto</button></form>'+
        '<p class="caption">Campi da completare prima di generare: '+
        esc((response.mancanti||[]).join(", ")||"nessuno")+'.</p>');
      document.getElementById("co-admin-form").onsubmit=async function(event){
        event.preventDefault();
        var button=this.querySelector("button[type=submit]");
        button.disabled=true;
        try{
          var result=await api("admin/co-veneto/config",{
            method:"PUT",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({dati:collect(this)})
          });
          note(result.mancanti.length?"Configurazione salvata: restano campi da verificare.":
            "Configurazione CO Veneto completa e salvata.",false);
          renderAdmin();
        }catch(error){note(error.message,true);button.disabled=false;}
      };
    }catch(error){html(panel("CO Veneto · Ateneo",esc(error.message)));}
  }
  function cardFor(job,data,admin,code,approvalId){
    var locked=!data.attiva||(job.ultimo_file&&!admin);
    var htmlParts='<div class="card mb" data-co-card="'+esc(job.incarico_id)+'">'+
      '<div class="actions" style="align-items:center;justify-content:space-between">'+
      '<div><strong>'+esc(job.cognome+" "+job.nome)+'</strong>'+
      '<div class="caption">ID PICA '+esc(job.id_pica)+' · '+esc(job.cds)+
      ' / '+esc(job.sede)+'</div></div>'+
      (data.attiva?'<label class="caption"><input type="checkbox" data-co-pick="'+
        esc(job.incarico_id)+'"'+(locked?' disabled':'')+'> Includi nello ZIP</label>':'')+
      '</div><p>'+esc(job.insegnamento)+' · '+esc(job.modulo)+'</p>'+
      '<p class="caption">Inizio da PICA: <strong>'+esc(job.inizio||"[DA VERIFICARE]")+
      '</strong> · Fine dall’A.A. '+esc(job.anno_accademico)+': <strong>'+
      esc(job.fine||"[DA VERIFICARE]")+'</strong> · Contratto B.03.00 - Co.co.co.</p>'+
      '<p class="caption">Residenza PICA (usata per precompilare il domicilio): '+
      esc([job.residenza_pica.indirizzo,job.residenza_pica.citta,job.residenza_pica.cap].
        filter(Boolean).join(", ")||"[DA VERIFICARE]")+'</p>';
    if(job.ultimo_file){
      htmlParts+='<p class="caption"><strong>XML generato, versione '+
        esc(job.ultimo_file.revisione)+'</strong> · '+esc(job.ultimo_file.creato_il)+
        ' · '+esc(job.ultimo_file.autore)+' · <a href="api/'+url(code,approvalId)+
        '/xml/'+encodeURIComponent(job.ultimo_file.id)+'">Scarica XML conservato</a></p>';
    }
    if(job.mancanti.length){
      htmlParts+='<p class="caption" style="color:#b25240">[DA VERIFICARE] '+
        esc(job.mancanti.join(", "))+'</p>';
    }
    htmlParts+='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:14px">'+
      contractFields.map(function(field){
        return inputFor(field[0],field[1],job.work[field[0]],locked);
      }).join("")+'</div>';
    if(!locked)htmlParts+='<div class="actions" style="margin-top:12px">'+
      '<button class="btn secondary" type="button" data-co-save="'+
      esc(job.incarico_id)+'">Salva dati incarico</button></div>';
    return htmlParts+'</div>';
  }
  async function renderApproval(){
    var code=state.active,approvalId=state.coApproval;
    if(!code||!approvalId){
      html(panel("CO Veneto","Apri un riquadro con ATTI APPROVATI da Graduatorie."));
      return;
    }
    html(panel("CO Veneto · Procedura "+esc(approvalId),"Caricamento vincitori..."));
    try{
      var data=await api(url(code,approvalId));
      if(state.page!=="coveneto"||state.active!==code)return;
      var admin=state.role==="admin",miss=data.configurazione_mancante||[];
      var cards=(data.incarichi||[]).map(function(job){
        return cardFor(job,data,admin,code,approvalId);
      }).join("");
      var history=(data.lotti||[]).map(function(lotto){
        return '<p><a href="api/'+url(code,approvalId)+'/lotti/'+
          encodeURIComponent(lotto.id)+'/zip">'+esc(lotto.nome_file)+'</a> · '+
          esc(lotto.creato_il)+' · '+esc(lotto.autore)+'</p>';
      }).join("");
      html('<div class="actions mb"><button class="btn secondary" id="co-back">Torna alle graduatorie</button>'+
        (admin?'<button class="btn secondary" id="co-admin-link">Configurazione Ateneo</button>':'')+
        '</div><div class="notice mb">'+
        (data.attiva?'Atti formalmente approvati: comunicazioni disponibili.':
          'Procedura non attiva o regredita: sola consultazione.')+
        ' RankUP non effettua invii: usare Verifica file sul portale CO Veneto.</div>'+
        (miss.length?'<p class="notice mb">Configurazione Ateneo incompleta: '+
          esc(miss.join(", "))+'</p>':'')+
        (cards||'<div class="card mb">Nessun vincitore individuato in questa approvazione.</div>')+
        (data.attiva?'<div class="card mb"><h3 class="section-heading">Genera ZIP UNILAV</h3>'+
          '<p class="caption">Salva i dati degli incarichi da includere. '+
          'Dopo la prima generazione gli User possono soltanto riscaricare il file; '+
          'la rigenerazione spetta all’Admin con motivazione.</p>'+
          (admin?'<label>Motivazione (obbligatoria in caso di rigenerazione)'+
            '<input id="co-reason" type="text" placeholder="Motivazione documentata" style="width:100%"></label>':'')+
          '<div class="actions"><button class="btn" id="co-generate"'+
          (miss.length?' disabled':'')+'>Genera ZIP CO Veneto</button></div></div>':'')+
        '<div class="card"><h3 class="section-heading">ZIP generati e conservati</h3>'+
        (history||'<p class="caption">Nessun file ancora generato.</p>')+'</div>');
      document.getElementById("co-back").onclick=function(){go("graduatorie");};
      if(document.getElementById("co-admin-link")){
        document.getElementById("co-admin-link").onclick=function(){go("coadmin");};
      }
      document.querySelectorAll("[data-co-save]").forEach(function(button){
        button.onclick=async function(){
          var card=button.closest("[data-co-card]");
          var jobId=Number(button.dataset.coSave);
          button.disabled=true;
          try{
            await api(url(code,approvalId)+"/incarichi/"+encodeURIComponent(jobId),{
              method:"PUT",headers:{"Content-Type":"application/json"},
              body:JSON.stringify({dati:collect(card)})
            });
            note("Dati del singolo incarico salvati.",false);
            renderApproval();
          }catch(err){note(err.message,true);button.disabled=false;}
        };
      });
      var generate=document.getElementById("co-generate");
      if(generate)generate.onclick=async function(){
        var ids=[...document.querySelectorAll("[data-co-pick]:checked")].
          map(function(box){return Number(box.dataset.coPick);});
        if(!ids.length){note("Seleziona almeno un incarico.",true);return;}
        if(!confirm("Generare "+ids.length+
          " XML e conservarli in uno ZIP? RankUP non trasmetterà alcuna comunicazione."))return;
        generate.disabled=true;
        try{
          var result=await api(url(code,approvalId)+"/genera",{
            method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({incarichi_ids:ids,motivazione:
              document.getElementById("co-reason")?
                document.getElementById("co-reason").value.trim():""})
          });
          note("ZIP creato e conservato. Scaricalo dallo storico ed esegui Verifica file sul portale.",false);
          renderApproval();
        }catch(err){note(err.message,true);generate.disabled=false;}
      };
    }catch(error){html(panel("CO Veneto",esc(error.message)));}
  }
  return {renderAdmin:renderAdmin,renderApproval:renderApproval};
};
})();
