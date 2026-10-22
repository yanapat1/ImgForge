#!/bin/bash
# SessionStart hook (Claude Code cloud sessions only): everything the test suite needs.
#   - .venv with requirements.txt + the test-only packages (pytest, moto, pyftpdlib, playwright for the
#     Chromium preinstalled in /opt/pw-browsers)
#   - PostgreSQL 16 with pgvector >= 0.7 (halfvec; Ubuntu ships 0.6, so 0.8.0 is built from source),
#     started on port 55433; DATAVIZ_TEST_DATABASE_URL points the tests at it
#   - npm packages + the built frontend (static/js, which the page tests load)
# Idempotent: every step is skipped when its result is already there.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"
PG_PORT=55433
PG_DIR=/var/tmp/pgtest
PLAYWRIGHT_VERSION="${PLAYWRIGHT_VERSION:-1.56.0}"     # its Chromium revision (1194) is the one in /opt/pw-browsers

# ---- Python
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt pytest "moto[server]" pyftpdlib \
  "playwright==${PLAYWRIGHT_VERSION}"

# ---- PostgreSQL 16 + pgvector 0.8
if ! command -v /usr/lib/postgresql/16/bin/pg_ctl >/dev/null || ! dpkg -s postgresql-server-dev-16 >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql-16 postgresql-server-dev-16 make gcc >/dev/null
fi
if [ ! -f /usr/share/postgresql/16/extension/vector--0.8.0.sql ]; then
  rm -rf /var/tmp/pgvector-src
  git clone -q --depth 1 --branch v0.8.0 https://github.com/pgvector/pgvector /var/tmp/pgvector-src
  make -s -C /var/tmp/pgvector-src OPTFLAGS="" >/dev/null
  make -s -C /var/tmp/pgvector-src install >/dev/null
fi
if [ ! -f "$PG_DIR/data/PG_VERSION" ]; then
  mkdir -p "$PG_DIR" && chown postgres "$PG_DIR"
  su postgres -c "/usr/lib/postgresql/16/bin/initdb -D $PG_DIR/data -A trust" >/dev/null
fi
if ! su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D $PG_DIR/data status" >/dev/null 2>&1; then
  su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D $PG_DIR/data -o '-p $PG_PORT -k $PG_DIR' -l $PG_DIR/log -w start" >/dev/null
fi
if [ -n "${CLAUDE_ENV_FILE:-}" ] && ! grep -q DATAVIZ_TEST_DATABASE_URL "$CLAUDE_ENV_FILE" 2>/dev/null; then
  echo "export DATAVIZ_TEST_DATABASE_URL=postgresql://postgres@localhost:${PG_PORT}/postgres" >> "$CLAUDE_ENV_FILE"
fi

# ---- frontend (TypeScript -> static/js)
npm install --no-audit --no-fund --loglevel=error >/dev/null
npm run -s build >/dev/null 2>&1
