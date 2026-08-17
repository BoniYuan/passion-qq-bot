#!/usr/bin/env sh
set -eu

if [ ! -f .env ]; then
  web_token="$(openssl rand -hex 32)"
  sed "s/change-this-to-a-long-random-string/$web_token/" .env.example > .env
  echo "Created .env with a random NapCat WebUI token."
else
  echo ".env already exists; keeping it."
fi

if command -v python3 >/dev/null 2>&1; then
  if key="$(python3 tools/generate_key.py 2>/dev/null)"; then
    printf 'AstrBot sub2 plugin encryption_key:\n%s\n' "$key" > setup-secrets.txt
    chmod 600 setup-secrets.txt
    echo "Created setup-secrets.txt. Enter its key in the sub2 plugin settings."
  else
    echo "Could not generate the plugin key. Run: python3 tools/generate_key.py"
  fi
fi
