#!/bin/bash
cd /workspaces/agent-crm/plus-agent
docker compose -f frappe_docker/pwd.yml up -d
docker start agent-redis 2>/dev/null || docker run -d --name agent-redis --restart unless-stopped -p 6379:6379 redis/redis-stack-server:latest
until docker exec agent-redis redis-cli ping 2>/dev/null | grep -q PONG; do sleep 2; echo "redis..."; done
until curl -sf http://localhost:8080/api/method/ping >/dev/null; do sleep 5; echo "erpnext..."; done
echo "LISTO"
exec uvicorn app.main:app --host 0.0.0.0 --port 8081
