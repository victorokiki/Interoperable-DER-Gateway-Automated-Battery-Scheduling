#!/usr/bin/env bash
# certs/generate_dev_certs.sh
# Generates a throwaway local CA + a server cert (for the fake grid
# server) + a client cert (for the EMS Bridge / IEEEClient) for testing
# NF-04 (TLS 1.2+ with mutual certificate authentication).
#
# These are DEV-ONLY certificates for exercising the mutual-TLS code
# path against the local simulator. Never use these in production —
# generate real certs from a proper CA for that.
#
# Usage:
#   cd certs && ./generate_dev_certs.sh

set -euo pipefail
cd "$(dirname "$0")"

DAYS=825

echo "== 1. Certificate Authority =="
openssl genrsa -out ca.key 2048
openssl req -x509 -new -nodes -key ca.key -sha256 -days $DAYS \
    -subj "/CN=Capstone Dev CA" -out ca.crt

echo "== 2. Server certificate (fake_grid_server.py, CN=localhost) =="
openssl genrsa -out server.key 2048
openssl req -new -key server.key -subj "/CN=localhost" -out server.csr
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days $DAYS -sha256 -out server.crt
rm -f server.csr

echo "== 3. Client certificate (EMS Bridge / IEEEClient) =="
openssl genrsa -out client.key 2048
openssl req -new -key client.key -subj "/CN=ems-bridge-client" -out client.csr
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days $DAYS -sha256 -out client.crt
rm -f client.csr

echo "== 4. An UNTRUSTED client cert (self-signed, NOT from our CA) =="
echo "   Used to prove the server actually rejects invalid certs."
openssl req -x509 -new -nodes -newkey rsa:2048 \
    -subj "/CN=untrusted-client" -days $DAYS -out untrusted_client.crt \
    -keyout untrusted_client.key

echo
echo "Done. Generated in $(pwd):"
ls -1 *.crt *.key
