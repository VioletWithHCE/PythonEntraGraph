#!/usr/bin/env bash
set -euo pipefail

CERT_NAME="ExchangeOnlineAutomation"
OUT_DIR="${1:-$HOME/exo-cert}"
DAYS_VALID=730

mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

echo "Creating private key and self-signed certificate in: $OUT_DIR"

openssl req -x509 -newkey rsa:2048 -sha256 \
  -keyout "${CERT_NAME}.key" \
  -out "${CERT_NAME}.crt" \
  -days "$DAYS_VALID" \
  -subj "/CN=${CERT_NAME}"

cp "${CERT_NAME}.crt" "${CERT_NAME}.cer"

echo
echo "Creating PFX/PKCS#12 bundle."
echo "OpenSSL will prompt you for an export password."
echo

openssl pkcs12 -export \
  -out "${CERT_NAME}.pfx" \
  -inkey "${CERT_NAME}.key" \
  -in "${CERT_NAME}.crt" \
  -name "${CERT_NAME}"

echo
echo "Done."
echo "Files created:"
echo "  $OUT_DIR/${CERT_NAME}.key"
echo "  $OUT_DIR/${CERT_NAME}.crt"
echo "  $OUT_DIR/${CERT_NAME}.cer"
echo "  $OUT_DIR/${CERT_NAME}.pfx"
echo

openssl x509 -in "${CERT_NAME}.crt" -noout -subject -dates -fingerprint
