FROM python:3.12-slim

# Sin buffer, o los logs no salen hasta que el proceso muere.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# No root: si algo se escapa del proceso, que no sea con root.
RUN useradd --create-home --uid 10001 agente && chown -R agente:agente /srv
USER agente

# Mismo puerto que start.sh y docker-compose.yml.
EXPOSE 8081

# Un solo worker a propósito: la cola durable en Redis y el worker con lease
# viven en el proceso (app/main.py). Si subís --workers, Redis tiene que
# seguir siendo compartido: idempotencia, locks y cola dependen de eso.
#
# --no-access-log: el access log de uvicorn incluye el query string, y la
# verificación única del webhook de Meta trae el verify token ahí.
# --proxy-headers: detrás del reverse proxy, que el cliente sea el real.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081", \
     "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
