# data stack

- **The data bucket**, named by `data_bucket_name` in `envs/<env>.tfvars` (prod:
  `cloud-pricing-data-prod-g08a9i`). The name is yours to choose and never includes the AWS
  account ID. Keep the `cloud-pricing-data-<env>-` prefix: the CI deploy role's permissions
  match it. The bucket is protected from `tofu destroy`, with
  Block Public Access, SSE-S3, TLS-only access and versioning (`main.tf`). Deleted or overwritten
  objects stay recoverable for `noncurrent_version_retention_days`.
- **Lifecycle rules** (`lifecycle.tf`): raw pricing files expire after `raw_retention_days`, one
  rule per provider.
- **The read-only policy** `cloud-pricing-data-read-<env>` (`read_policy.tf`): attach it to the web
  app's role inside AWS.
- **Off-AWS access** (`external_access.tf`, optional): short-lived credentials for machines
  outside AWS through IAM Roles Anywhere. Setup is below.

## Off-AWS access (IAM Roles Anywhere)

A home server can publish snapshots (the **writer** role), and a laptop or other machine can read
them (the **reader** role), with no long-lived AWS keys. Each machine holds a client certificate
issued by your own CA. AWS exchanges it for temporary credentials. Running your own CA costs
nothing; AWS Private CA would cost $50–400/month.

### 1. Create the CA (once)

Keep the CA key **offline**, for example on an encrypted USB drive. It never goes into the repo or
AWS.

```bash
mkdir -p ~/cloud-pricing-ca && cd ~/cloud-pricing-ca
openssl ecparam -genkey -name prime256v1 -out ca.key
openssl req -x509 -new -key ca.key -sha256 -days 3650 -out ca.pem \
  -subj "/CN=cloud-pricing owner CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
```

### 2. Issue a client certificate per machine (valid ≤ 1 year)

The CN names the machine. It's what you allow and revoke.

```bash
CN=home-server
openssl ecparam -genkey -name prime256v1 -out $CN.key
openssl req -new -key $CN.key -out $CN.csr -subj "/CN=$CN"
openssl x509 -req -in $CN.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 365 -sha256 \
  -out $CN.pem -extfile <(printf "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth")
```

Copy only `$CN.pem` and `$CN.key` to that machine.

### 3. Allow the machines and apply

Put the CA's **public** certificate and the CNs in `infra/envs/<env>.tfvars`:

```hcl
roles_anywhere_ca_bundle_pem = <<-EOT
  -----BEGIN CERTIFICATE-----
  ...contents of ca.pem...
  -----END CERTIFICATE-----
EOT
external_writer_subjects = ["home-server"]
external_reader_subjects = ["dev-laptop"]
```

Apply the `data` stack, then the `pipeline` stack. The pipeline stack grants the writer role
alert publishing and image pulls. Note these outputs: `roles_anywhere_trust_anchor_arn`,
`writer_role_arn`, `writer_profile_arn`, `reader_role_arn` and `reader_profile_arn`.

### 4. Configure the machine

Create `~/.pricing/aws-config`. For a reader, use the reader ARNs and certificate:

```ini
[profile pricing-writer]
region = us-east-1
credential_process = aws_signing_helper credential-process --certificate /creds/home-server.pem --private-key /creds/home-server.key --trust-anchor-arn <roles_anywhere_trust_anchor_arn> --profile-arn <writer_profile_arn> --role-arn <writer_role_arn>
```

Put `home-server.pem` and `home-server.key` in `~/.pricing/` too. Inside the container they're
mounted at `/creds`. The pipeline image already includes `aws_signing_helper`. To use the AWS CLI
directly on the host, install the helper there and use host paths.

### 5. Run the pipeline on the home server

Pause the Fargate schedule first (`schedule_enabled = false` in the pipeline stack), so only one
writer runs. The run claim would refuse a concurrent second run anyway.

```bash
docker run --rm \
  -v ~/.pricing:/creds:ro \
  -e AWS_CONFIG_FILE=/creds/aws-config -e AWS_PROFILE=pricing-writer \
  -e PIPELINE_STORAGE_URI=s3://cloud-pricing-data-prod-g08a9i/ \
  -e PIPELINE_ENVIRONMENT=prod -e PIPELINE_HOST_LABEL=home-server \
  -e ALERT_TOPIC_ARN=<alert topic ARN> \
  <account_id>.dkr.ecr.us-east-1.amazonaws.com/cloud-pricing-pipeline-prod:<sha> \
  run --trigger scheduled
```

To pull the image, log in to ECR with the same profile:
`aws ecr get-login-password --profile pricing-writer | docker login --username AWS --password-stdin <account_id>.dkr.ecr.us-east-1.amazonaws.com`.

Schedule it weekly with a systemd timer:

```ini
# /etc/systemd/system/cloud-pricing.service
[Unit]
Description=Cloud pricing weekly snapshot
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/cloud-pricing-run.sh   # the docker run command above

# /etc/systemd/system/cloud-pricing.timer
[Timer]
OnCalendar=Mon *-*-* 13:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

Or with cron: `0 13 * * 1 /usr/local/bin/cloud-pricing-run.sh` (with the host clock in UTC).

### Keep the clock right

Run claims and grace periods compare the host's clock with S3's. A run refuses to start (exit 2)
if they differ by more than `MAX_CLOCK_SKEW_SECONDS` (default 300). Keep NTP running:
`timedatectl set-ntp true`.

### Alerts off AWS

The run's own alerts (failed, partial, refused, crashed, retention error) work the same as in the
cloud. A hard kill, such as out-of-memory or a power loss, can't send anything. Only the daily
missed-run watchdog catches it. While the home server is the writer, consider lowering
`max_snapshot_age_days`, for example to 7.

### Rotate a certificate (yearly)

Issue a new certificate with the **same CN** (step 2), replace the files on the machine, then
delete the old ones. No apply is needed, because the CN didn't change. The CA certificate itself
is valid for 10 years. To replace the CA, add the new CA's PEM to `roles_anywhere_ca_bundle_pem`
alongside the old one, re-issue the client certificates, and then remove the old CA.

### Revoke a machine

- **Immediately:** remove its CN from `external_*_subjects` and apply the data stack.
- **Just one certificate** (keeping the CN): revoke it in a CRL and import the CRL. One-time CRL
  setup in the CA folder:

  ```bash
  cat > ca.cnf <<'EOF'
  [ ca ]
  default_ca = owner_ca
  [ owner_ca ]
  dir              = .
  database         = $dir/index.txt
  crlnumber        = $dir/crlnumber
  certificate      = $dir/ca.pem
  private_key      = $dir/ca.key
  default_md       = sha256
  default_crl_days = 365
  EOF
  touch index.txt && echo 01 > crlnumber
  ```

  Then revoke and publish:

  ```bash
  openssl ca -config ca.cnf -revoke home-server.pem
  openssl ca -config ca.cnf -gencrl -out crl.pem
  aws rolesanywhere import-crl --name cloud-pricing-crl-prod \
    --crl-data fileb://crl.pem --trust-anchor-arn <roles_anywhere_trust_anchor_arn> --enabled
  ```

  For later revocations, update the imported CRL with `aws rolesanywhere update-crl`.
