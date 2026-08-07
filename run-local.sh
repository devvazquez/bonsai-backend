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
  echo "Ábrelo, pon tu GROQ_API_KEY y vuelve a ejecutar este script."
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
PROVEEDOR="${VISION_PROVIDER:-groq}"
if [ "$PROVEEDOR" = "groq" ]; then
  CLAVE="${GROQ_API_KEY:-}"; ESPERADO="la-teva-api-key-de-groq"; NOMBRE="GROQ_API_KEY"
else
  CLAVE="${GEMINI_API_KEY:-}"; ESPERADO="tu-api-key-de-gemini"; NOMBRE="GEMINI_API_KEY"
fi
if [ -z "$CLAVE" ] || [ "$CLAVE" = "$ESPERADO" ]; then
  echo
  echo "Falta $NOMBRE en el .env: /api/v1/look dará error 500."
  echo "El resto (/api/v1/speak, /memory, /provar) sí funcionará:"
  echo "la voz es Piper y va en local, sin ninguna clave."
fi

# Por defecto solo escucha en este ordenador. Para abrir /provar desde el
# móvil hace falta que escuche también en la red local:
#     BONSAI_HOST=0.0.0.0 ./run-local.sh
HOST="${BONSAI_HOST:-127.0.0.1}"

echo
echo "  Servidor:      http://127.0.0.1:8080"
echo "  API:           http://127.0.0.1:8080/api/v1"
echo "  Hacer una foto: http://127.0.0.1:8080/provar"
echo "  Base de datos: http://127.0.0.1:8080/admin  (solo con ADMIN_PASSWORD)"
echo "  Documentación: http://127.0.0.1:8080/docs"
if [ "$HOST" = "0.0.0.0" ]; then
  IP=$(hostname -I 2>/dev/null | awk "{print \$1}")
  [ -z "$IP" ] && IP=$(ipconfig getifaddr en0 2>/dev/null || true)
  [ -n "$IP" ] && echo "  Desde el móvil: http://$IP:8080/provar  (misma WiFi)"
else
  echo "  Desde el móvil: BONSAI_HOST=0.0.0.0 ./run-local.sh"
fi
echo "  Para probarlo: python test_bonsai.py  (en otra terminal)"
echo "  Ctrl+C para parar."
echo

exec "$PY" -m uvicorn main:app --host "$HOST" --port 8080 --reload
