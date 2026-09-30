#!/bin/sh
# Restores the newest backup into a scratch database and compares row counts with the live
# database. Prints the backup age (what a restore would lose, RPO) and the restore time (RTO).
#   docker compose -f docker-compose.ft.yml exec backup sh /backup/restore_check.sh
set -eu
latest=$(ls -1t /backups/*.dump | head -1)
age=$(( $(date +%s) - $(stat -c %Y "$latest") ))
psql -d postgres -qc "DROP DATABASE IF EXISTS restore_check"
psql -d postgres -qc "CREATE DATABASE restore_check"
start=$(date +%s%N)
pg_restore -d restore_check --no-owner "$latest"
restore_ms=$(( ($(date +%s%N) - start) / 1000000 ))
for table in student.students student.registrations payment.payments records.grades; do
    restored=$(psql -d restore_check -tAc "SELECT count(*) FROM $table")
    live=$(psql -tAc "SELECT count(*) FROM $table")
    echo "{\"table\": \"$table\", \"restored\": $restored, \"live\": $live}"
done
psql -d postgres -qc "DROP DATABASE restore_check"
echo "{\"backup\": \"$latest\", \"backup_age_s\": $age, \"restore_ms\": $restore_ms}"
