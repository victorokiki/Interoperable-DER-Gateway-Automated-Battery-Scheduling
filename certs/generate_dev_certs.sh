#!/usr/bin/env bash
# certs/generate_dev_certs.sh
# Generates a throwaway local CA + a server cert (for the fake grid
# server) + a client cert (for the EMS Bridge / IEEEClient) for testing
# NF-04 (TLS 1.2+ with mutual certificate authentication).
#
# The keys are ECDSA on P-256 (prime256v1) with SHA-256 signatures, the
# kind IEEE 2030.5 device certificates use; the IEEE 2030.5 cipher suite
# TLS_ECDHE_ECDSA_WITH_AES_128_CCM_8 needs an ECDSA server certificate.
#
# The CA carries keyUsage, and the leaves keyUsage and extendedKeyUsage,
# so strict X.509 checking (Python 3.13+ ssl.create_default_context)
# accepts them.
#
# These are DEV-ONLY certificates for exercising the mutual-TLS code
# path against the local simulator. Never use these in production —
# generate real certs from a proper CA for that.
#
# Usage:
#   cd certs && ./generate_dev_certs.sh

set -euo pipefail
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: keep "/CN=..." as it is

DAYS=825

new_key() {
    openssl ecparam -name prime256v1 -genkey -noout -out "$1"
}

echo "== 1. Certificate Authority =="
new_key ca.key
openssl req -x509 -new -key ca.key -sha256 -days $DAYS \
    -subj "/CN=Capstone Dev CA" -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca.crt

echo "== 2. Server certificate (fake_grid_server.py, localhost / 127.0.0.1) =="
new_key server.key
openssl req -new -key server.key -subj "/CN=localhost" -out server.csr
printf "subjectAltName=DNS:localhost,IP:127.0.0.1\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n" > server.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days $DAYS -sha256 -extfile server.ext -out server.crt
rm -f server.csr server.ext

echo "== 3. Client certificate (EMS Bridge / IEEEClient) =="
new_key client.key
openssl req -new -key client.key -subj "/CN=ems-bridge-client" -out client.csr
printf "keyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n" > client.ext
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days $DAYS -sha256 -extfile client.ext -out client.crt
rm -f client.csr client.ext

echo "== 4. An UNTRUSTED client cert (self-signed, NOT from our CA) =="
echo "   Used to prove the server actually rejects invalid certs."
new_key untrusted_client.key
openssl req -x509 -new -key untrusted_client.key -sha256 \
    -subj "/CN=untrusted-client" -days $DAYS -out untrusted_client.crt

echo
echo "Done. Generated in $(pwd):"
ls -1 *.crt *.key
