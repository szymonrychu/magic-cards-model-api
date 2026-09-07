#!/bin/sh
# Echo the local development postgres password, generating one on first use.
#
# No password literal is committed anywhere in this repo. A hardcoded default
# in docker-compose.yml would be flagged by secret scanning on every pull
# request, and a security check that is always red is a security check nobody
# reads. It is also a habit worth not carrying into the six repos phase 2b
# creates, each of which gets its own compose file.
#
# The value is generated once and kept, because postgres bakes the password
# into the data directory the first time the volume is initialised: a fresh
# password on every run would authenticate fine once and then fail against the
# volume it already created.
#
# POSTGRES_PASSWORD in the environment (from a gitignored .env, loaded by mise)
# always wins.
set -eu

if [ -n "${POSTGRES_PASSWORD:-}" ]; then
  printf '%s' "$POSTGRES_PASSWORD"
  exit 0
fi

root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
file="$root/.postgres-password"

if [ ! -s "$file" ]; then
  (umask 077; LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 32 > "$file")
fi

printf '%s' "$(cat "$file")"
