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

EXPOSE 8080

# Un solo worker a propósito. El trabajo pesado (LLM + REST) ya sale del
# event loop por BackgroundTasks, que corre en el threadpool de Starlette,
# así que un worker atiende varias conversaciones en paralelo. Si hace falta
# más, subí --workers Y ASEGURATE de que Redis siga siendo compartido: la
# idempotencia y el lock de auto-confirmación dependen de eso.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
