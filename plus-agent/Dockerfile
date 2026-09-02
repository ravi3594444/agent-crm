FROM python:3.12-slim
# Operational log lines are print()s; keep them unbuffered so `docker logs`
# shows them as they happen.
ENV PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Uvicorn's default access log includes query strings. Meta's one-time webhook
# verification request contains the verify token in that query string.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
