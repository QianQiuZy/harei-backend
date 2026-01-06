#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
USERNAME="${AUTH_USERNAME:-admin}"
PASSWORD="${AUTH_PASSWORD:-password}"

echo "== 登录获取 Token =="
LOGIN_JSON=$(curl -sS -X POST "${BASE_URL}/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${USERNAME}\",\"password\":\"${PASSWORD}\"}")
TOKEN=$(env LOGIN_JSON="${LOGIN_JSON}" python - <<'PY'
import json, os
payload = json.loads(os.environ["LOGIN_JSON"])
print(payload.get("token", ""))
PY
)
if [[ -z "${TOKEN}" ]]; then
  echo "登录失败，请检查 AUTH_USERNAME / AUTH_PASSWORD 配置。"
  exit 1
fi

echo "== /auth =="
curl -sS -H "Authorization: Bearer ${TOKEN}" "${BASE_URL}/auth" | python -m json.tool

echo "== /music =="
curl -sS "${BASE_URL}/music" | python -m json.tool

echo "== /huangdou/rank =="
curl -sS "${BASE_URL}/huangdou/rank" | python -m json.tool

echo "== /huangdou/uid?uid=demo =="
curl -sS "${BASE_URL}/huangdou/uid?uid=demo" | python -m json.tool || true

echo "== /tag/active =="
curl -sS "${BASE_URL}/tag/active" | python -m json.tool

echo "== /captains =="
curl -sS -H "Authorization: Bearer ${TOKEN}" "${BASE_URL}/captains" | python -m json.tool

echo "== /captaingift =="
curl -sS "${BASE_URL}/captaingift" | python -m json.tool

echo "== /box/pending =="
curl -sS -H "Authorization: Bearer ${TOKEN}" "${BASE_URL}/box/pending" | python -m json.tool

echo "== /logout =="
curl -sS -X POST -H "Authorization: Bearer ${TOKEN}" "${BASE_URL}/logout" | python -m json.tool
