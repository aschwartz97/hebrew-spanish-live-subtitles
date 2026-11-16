#!/bin/bash
# Doble clic para arrancar los subtítulos (la primera vez: chmod +x translator.command)
cd "$(dirname "$0")"
source .venv/bin/activate
(sleep 2 && open -a "Google Chrome" http://localhost:8765) &
python server.py
