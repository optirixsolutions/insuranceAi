#!/bin/sh
# Starts a single-node SeaweedFS with the S3 gateway, using credentials from the environment.
set -eu

: "${S3_ACCESS_KEY_ID:?S3_ACCESS_KEY_ID must be set}"
: "${S3_SECRET_ACCESS_KEY:?S3_SECRET_ACCESS_KEY must be set}"

umask 077
cat > /tmp/s3.json <<EOF
{
  "identities": [
    {
      "name": "claimos",
      "credentials": [{ "accessKey": "${S3_ACCESS_KEY_ID}", "secretKey": "${S3_SECRET_ACCESS_KEY}" }],
      "actions": ["Admin", "Read", "Write", "List", "Tagging"]
    }
  ]
}
EOF

exec weed server \
  -dir=/data \
  -ip=127.0.0.1 \
  -ip.bind=0.0.0.0 \
  -master.volumeSizeLimitMB=512 \
  -volume.max=0 \
  -s3 \
  -s3.port=8333 \
  -s3.config=/tmp/s3.json
