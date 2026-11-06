#!/bin/sh
set -eu

MODEL="qwen2.5:1.5b-instruct-q4_K_M"
mkdir -p /data/models
ollama serve &
server_pid=$!

cleanup() {
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
}
trap cleanup INT TERM

ready=0
i=0
while [ "$i" -lt 60 ]; do
  if ollama list >/dev/null 2>&1; then
    ready=1
    break
  fi
  i=$((i + 1))
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo "Ollama non disponibile: controllare i log dell'add-on."
  cleanup
  exit 1
fi

# Il modello rimane in /data e non viene riscaricato ad ogni aggiornamento.
if ! ollama list | grep -Fq "$MODEL"; then
  echo "Download iniziale del modello $MODEL (solo pesi pubblici)."
  ollama pull "$MODEL" || { echo "Download modello non riuscito"; cleanup; exit 1; }
fi
echo "RankUP Ollama pronto. Il modello si scarica dalla RAM dopo ogni richiesta."
wait "$server_pid"
