FROM python:3.12-slim

WORKDIR /app

# Dependencias primero, para aprovechar la caché de Docker en los rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Directorio para la base de datos de memoria (monta aquí un volumen
# persistente si quieres que los recuerdos sobrevivan a los redespliegues).
RUN mkdir -p /data

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
