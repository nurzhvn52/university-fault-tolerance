#!/bin/sh
# Runs once on the node that initialises the cluster; $1 is a connection string.
set -e
psql "$1" -v ON_ERROR_STOP=1 \
    -c "CREATE ROLE uft LOGIN CREATEDB PASSWORD '${UFT_DB_PASSWORD}'" \
    -c "CREATE DATABASE university OWNER uft"
# One database role per service, restricted to its own schema by the 0003 migration.
for service in student payment records timetable auth; do
    psql "$1" -v ON_ERROR_STOP=1 \
        -c "CREATE ROLE svc_${service} LOGIN PASSWORD '${UFT_SVC_PASSWORD}'"
done
