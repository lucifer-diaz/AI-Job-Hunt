#!/usr/bin/env bash
# Create the local Postgres databases for Job Hunt OS. Safe to run repeatedly.
#
#   scripts/setup_local_postgres.sh            # uses $USER, localhost:5432
#   PGUSER=rohanroy PGHOST=localhost scripts/setup_local_postgres.sh
set -euo pipefail

PGUSER="${PGUSER:-$USER}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
DATABASES=("${JOBHUNT_DB:-jobhunt}" "${JOBHUNT_TEST_DB:-jobhunt_test}")

if ! command -v psql >/dev/null; then
  echo "psql not found. Install Postgres, e.g. 'brew install postgresql@16 && brew services start postgresql@16'." >&2
  exit 1
fi
if ! pg_isready -h "$PGHOST" -p "$PGPORT" >/dev/null; then
  echo "Postgres is not accepting connections on $PGHOST:$PGPORT. Start it, e.g. 'brew services start postgresql@16'." >&2
  exit 1
fi

for db in "${DATABASES[@]}"; do
  exists=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$db'")
  if [[ "$exists" == "1" ]]; then
    echo "exists:  $db"
  else
    createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -O "$PGUSER" "$db"
    echo "created: $db"
  fi
done

cat <<EOF

Add to .env:
  DATABASE_URL=postgresql+psycopg://$PGUSER@$PGHOST:$PGPORT/${DATABASES[0]}

Optional, to run the test suite on Postgres:
  TEST_DATABASE_URL=postgresql+psycopg://$PGUSER@$PGHOST:$PGPORT/${DATABASES[1]} pytest

Tables are created and migrated automatically when the app starts.
EOF
