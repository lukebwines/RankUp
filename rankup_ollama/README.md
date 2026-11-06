# RankUP Local AI (Ollama)

Servizio opzionale per Home Assistant OS: modello Qwen2.5 1.5B quantizzato, eseguito localmente.

- Download del modello solo alla prima installazione; i pesi rimangono su /data/models.
- Nessuna porta esposta sulla LAN per impostazione predefinita.
- Le richieste di RankUP restano nella rete interna degli add-on.
- Modello scaricato dalla RAM dopo ogni richiesta (OLLAMA_KEEP_ALIVE=0).
- Nessun accesso alle entità Home Assistant o alle domande PICA.

## Configurazione

Installare l'add-on **RankUP Local AI (Ollama)** dallo stesso repository di RankUP e avviarlo.
Non abilitare la porta host 11434.

In **RankUP → Configurazione** impostare `ollama_url` all'URL interno dell'add-on, ad esempio
`http://<nome-host-interno-ollama>:11434` (il nome dipende dal codice attribuito
da Home Assistant al repository, non è fisso). Nomi e identificativi interni si possono
verificare tramite il terminale `ha apps info <slug-completo>` oppure nello stato
degli add-on. Il nome host di rete usa di norma i trattini al posto degli underscore.

Nella pagina **Incarichi**, premere **Verifica Ollama locale**. Dopo aver caricato il
PDF ufficiale, selezionare un CdS e premere **Suggerisci requisiti con IA locale**.
Confermare singolarmente ogni proposta soltanto dopo verifica del bando.

Non sostituire l'indirizzo con il dominio Cloudflare né pubblicare Ollama su Internet.
