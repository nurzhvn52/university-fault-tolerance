#!/bin/sh
# Runs once on the node that initialises the cluster; $1 is a connection string.
set -e
psql "$1" -v ON_ERROR_STOP=1 \
    -c "CREATE ROLE uft LOGIN CREATEDB PASSWORD '${UFT_DB_PASSWORD}'" \
    -c "CREATE DATABASE university OWNER uft"
