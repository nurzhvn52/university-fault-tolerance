#!/bin/sh
# Logical backup of the university database every BACKUP_INTERVAL_S seconds (connection
# settings from the PG* environment); keeps the five newest dumps.
set -u
mkdir -p /backups
while true; do
    started=$(date +%s)
    file=/backups/university-$(date -u +%Y%m%dT%H%M%SZ).dump
    if pg_dump -Fc -f "$file.part" && mv "$file.part" "$file"; then
        echo "{\"event\": \"backup_completed\", \"file\": \"$file\", \"seconds\": $(( $(date +%s) - started ))}"
    else
        rm -f "$file.part"
        echo "{\"event\": \"backup_failed\"}"
    fi
    ls -1t /backups/*.dump 2>/dev/null | tail -n +6 | xargs -r rm -f
    sleep "${BACKUP_INTERVAL_S:-60}"
done
