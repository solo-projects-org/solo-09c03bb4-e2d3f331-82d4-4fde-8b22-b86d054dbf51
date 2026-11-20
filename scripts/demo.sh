#!/usr/bin/env bash
# 演示：底表 -> 对照 v1/v2/v3 -> 核验单登记/撤销 -> 凭证签发（撤单与签发交错、换版冲突）。
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
echo "##### 2. 提交对照 v1：C、D 经对照相连，并为一组（组 3，2023 合计 700）"
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
echo "##### 4. 登记核验单：合计须与底表相符（最后一单金额不符 -> 422 拒收）"
req POST /forms '{"form_no":"F-C","year":2023,"tax_codes":["C"],"total_cents":300}'
req POST /forms '{"form_no":"F-D","year":2023,"tax_codes":["D"],"total_cents":400}'
req POST /forms '{"form_no":"F-CD","year":2023,"tax_codes":["C","D"],"total_cents":700}'
req POST /forms '{"form_no":"F-CD24","year":2024,"tax_codes":["C","D"],"total_cents":720}'
req POST /forms '{"form_no":"F-BAD","year":2023,"tax_codes":["C"],"total_cents":999}'

echo
echo "##### 5. 预演选单（不写凭证）：F-C+F-D 两张 vs F-CD 一张，取最少张 F-CD"
req POST /vouchers/preview '{"observed_version":1,"group_id":3,"year":2023,
  "form_nos":["F-C","F-D","F-CD"]}'

echo
echo "##### 6. 正式签发 V000001：一张整用，恰好覆盖组 3 的 2023 年成员"
req POST /vouchers '{"observed_version":1,"group_id":3,"year":2023,
  "form_nos":["F-C","F-D","F-CD"]}'

echo
echo "##### 7. 撤单/签发交错：撤销未入选的 F-C 后，F-CD 仍可单独覆盖 -> 201（忽略撤销单）"
req POST /forms/F-C/revoke
req POST /vouchers '{"observed_version":1,"group_id":3,"year":2023,
  "form_nos":["F-CD","F-C"]}'

echo
echo "##### 7b. 同年另签 2024 年凭证（F-CD24，合计 720）"
req POST /vouchers '{"observed_version":1,"group_id":3,"year":2024,
  "form_nos":["F-CD24"]}'

echo
echo "##### 8. 撤销入选所需的 F-CD 后再签发 -> 409 冲突，不生成凭证"
req POST /forms/F-CD/revoke
req POST /vouchers '{"observed_version":1,"group_id":3,"year":2023,
  "form_nos":["F-CD","F-D"]}'

echo
echo "##### 9. 改版 v2：把 C/D 组拆回单税号组（组 3 撤下，新组 4、5）"
req POST /mappings '{"version":2,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 10. 换版后所见版过期：旧组 3 已不存在 -> 422；再交 v3 重新并组（新组 6）"
req POST /vouchers '{"observed_version":1,"group_id":3,"year":2023,"form_nos":["F-D"]}'
req POST /mappings '{"version":3,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"C","new_code":"D"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 11. 组 6 存在但所见版仍是 v2 -> 409；补登记新单后按现行 v3 签发 -> 201"
req POST /forms '{"form_no":"G-C","year":2023,"tax_codes":["C"],"total_cents":300}'
req POST /forms '{"form_no":"G-D","year":2023,"tax_codes":["D"],"total_cents":400}'
req POST /vouchers '{"observed_version":2,"group_id":6,"year":2023,
  "form_nos":["G-C","G-D"]}'
req POST /vouchers '{"observed_version":3,"group_id":6,"year":2023,
  "form_nos":["G-C","G-D"]}'

echo
echo "##### 12. 按版取供数变更"
req GET /changes/1
req GET /changes/2

echo
echo "##### 13. 旧版重传：原内容 -> 200 返回当时的变更；改内容 -> 409"
req POST /mappings '{"version":1,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"C","new_code":"D"},
  {"old_code":"D","new_code":"D"}]}'
req POST /mappings '{"version":1,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 14. 对照漏掉底表税号 -> 422 整版拒收，原供数仍可查询"
req POST /mappings '{"version":4,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"}]}'
req GET /summary

echo
echo "##### 15. 凭号查询、凭证清单与撤销单留痕"
req GET /vouchers/V000001
req GET /vouchers
req GET '/forms?status=revoked'
req GET /versions
