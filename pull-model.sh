#!/bin/bash
# Pull Ollama model - run this as a one-time Railway job after deploying the ollama service

set -e

OLLAMA_HOST="${OLLAMA_HOST:-http://ollama:11434}"
MODEL="${OLLAMA_MODEL:-gemma4:12b-it-qat}"

echo "Waiting for Ollama to be ready at $OLLAMA_HOST..."
for i in {1..30}; do
    if curl -sf "$OLLAMA_HOST/api/tags" > /dev/null; then
        echo "Ollama is ready!"
        break
    fi
    echo "Waiting... ($i/30)"
    sleep 2
done

echo "Pulling model: $MODEL"
ollama pull "$MODEL"

echo "Model pulled successfully!"