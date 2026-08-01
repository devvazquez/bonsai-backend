#!/usr/bin/env bash
# Arranca el backend de Bonsai en local, sin Docker (Linux / macOS).
#
#   ./run-local.sh
#
# Crea el entorno virtual la primera vez, instala las dependencias, carga el
# .env y levanta el servidor en http://127.0.0.1:8080 con recarga automática.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "He creado el fichero .env a partir de .env.example."
  echo "Ábrelo, pon tu GEMINI_API_KEY y vuelve a ejecutar este script."
  echo
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creando el entorno virtual (.venv)..."
  python3 -m venv .venv
fi

PY=.venv/bin/python

echo "Instalando dependencias..."
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt

# Carga el .env en las variables de entorno de este proceso.
set -a
# shellcheck disable=SC1091
. ./.env
set +a

# Solo se avisa de la clave del proveedor que se vaya a usar de verdad.
PROVEEDOR="${VISION_PROVIDER:-gemini}"
if [ "$PROVEEDOR" = "groq" ]; then
  CLAVE="${GROQ_API_KEY:-}"; ESPERADO="tu-api-key-de-groq"; NOMBRE="GROQ_API_KEY"
else
  CLAVE="${GEMINI_API_KEY:-}"; ESPERADO="tu-api-key-de-gemini"; NOMBRE="GEMINI_API_KEY"
fi
if [ -z "$CLAVE" ] || [ "$CLAVE" = "$ESPERADO" ]; then
  echo
  echo "Falta $NOMBRE en el .env: /look dará error 500."
  echo "El resto (/speak, /memory, /voices, /provar) sí funcionará:"
  echo "la voz es Piper y va en local, sin ninguna clave."
fi

echo
echo "  Servidor:      http://127.0.0.1:8080"
echo "  Desde el móvil: http://127.0.0.1:8080/provar"
echo "  Base de datos: http://127.0.0.1:8080/admin  (solo con ADMIN_PASSWORD)"
echo "  Documentación: http://127.0.0.1:8080/docs"
echo "  Para probarlo: python test_bonsai.py  (en otra terminal)"
echo "  Ctrl+C para parar."
echo

exec "$PY" -m uvicorn main:app --host 127.0.0.1 --port 8080 --reload
