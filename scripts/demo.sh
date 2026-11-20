#!/usr/bin/env bash
# 演示：固定底表 -> 提交 v1（C/D 并成一组）-> v2 拆组 -> 按版取变更 -> 旧版重传与冲突。
# 用法: ./scripts/demo.sh [BASE_URL]   默认 http://localhost:8000
set -euo pipefail

BASE="${1:-http://localhost:8000}"

req() { # req METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  echo
  echo "=== $method $path ${body:+-- $body}"
  if [ -n "$body" ]; then
    curl -sS -w '\nHTTP %{http_code}\n' -X "$method" "$BASE$path" \
      -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -w '\nHTTP %{http_code}\n' -X "$method" "$BASE$path"
  fi
}

echo "##### 1. 固定相邻两年底表（2023/2024，整数分）"
req PUT /base '{"amounts":[
  {"year":2023,"code":"A","amount_cents":100},
  {"year":2023,"code":"B","amount_cents":200},
  {"year":2023,"code":"C","amount_cents":300},
  {"year":2023,"code":"D","amount_cents":400},
  {"year":2024,"code":"A","amount_cents":110},
  {"year":2024,"code":"B","amount_cents":210},
  {"year":2024,"code":"C","amount_cents":310},
  {"year":2024,"code":"D","amount_cents":410}]}'

echo
echo "##### 2. 提交对照 v1：C、D 经对照相连，并为一组"
req POST /mappings '{"version":1,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"C","new_code":"D"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 3. 现行汇总（组 3 = {C,D | C,D}，各年合计 700/720）"
req GET /summary

echo
echo "##### 4. 改版 v2：把 C/D 组拆回单税号组"
req POST /mappings '{"version":2,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 5. 按版取供数变更"
req GET /changes/1
req GET /changes/2

echo
echo "##### 6. 旧版重传：原内容 -> 200 返回当时的变更"
req POST /mappings '{"version":1,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"C","new_code":"D"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 7. 旧版改内容 -> 409 冲突"
req POST /mappings '{"version":1,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 8. 对照漏掉底表税号 -> 422 整版拒收，原供数仍可查询"
req POST /mappings '{"version":3,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"}]}'
req GET /summary

echo
echo "##### 9. 版本列表"
req GET /versions
