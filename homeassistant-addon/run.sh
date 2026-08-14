#!/bin/sh
set -e

if [ -f /data/options.json ]; then
  IS74_INTERNAL_BASE_URL=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('internal_base_url') or '')")
  export IS74_INTERNAL_BASE_URL
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8090
