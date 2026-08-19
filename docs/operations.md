# Operations

Adaptive Tutor supports a hardened Docker Compose deployment and native
systemd services. Both paths keep SQLite on persistent storage and restart the
webhook service, durable worker, and isolated grader after a crash or reboot.
Learner code runs only on credential-free ephemeral evaluators, never on the
tutor host.

## Docker Compose

### Install and initialize

From a release checkout:

```bash
cd deploy
./prepare-compose.sh
docker compose build
docker compose --profile tools run --rm initializer
```

`prepare-compose.sh` creates owner-only `runtime/config`, `runtime/state`,
`runtime/codex`, and `runtime/grader-run` directories, three mode-0600
environment files, and a local Compose UID/GID mapping. Initialization writes
`runtime/config/config.yaml`, a mode-0600 token file, the migrated SQLite
database, and the bundled neutral curriculum.

The container binds to `0.0.0.0` internally, but Compose publishes it only on
host loopback at `127.0.0.1:8765`. The dashboard still requires the generated
token. Retrieve that token locally from `runtime/state/secrets.env`; do not put
it in shell history, chat, source control, or a reverse-proxy configuration.

Run the credential-free product first:

```bash
docker compose up -d tutor
docker compose ps
curl --fail http://127.0.0.1:8765/readyz
docker compose logs --tail=100 tutor
```

### Verify a deployed runtime

The repository includes a destructive-to-itself, disposable Compose proof. It
builds the current image, initializes temporary state, verifies health and API
authorization, inspects runtime hardening and loopback publication, terminates
the service process to prove automatic restart and state recovery, creates an
integrity-checked backup, writes sanitized evidence, and removes the temporary
project:

```bash
./scripts/prove-deployed-runtime
./scripts/check-operational-evidence
```

Run it from a clean release checkout with a working Docker daemon. The
`--skip-build` option is only for a locally prepared `adaptive-tutor:local`
image; the prover rejects that image unless its source-revision label exactly
matches the checkout.

Use an SSH tunnel or authenticated private reverse proxy for remote access.
Keep the published Compose port loopback-bound. If TLS terminates at a proxy,
allow only trusted users and preserve the service's security headers.

### Enable GitHub and model grading

Edit `runtime/config/config.yaml` and set the GitHub owner, private workspace,
GitHub App ID, installation ID, HTTPS webhook URL, and container path to the App
key. Store that key under `runtime/config` with mode `0600`; its path inside the
container starts with `/etc/adaptive-tutor/`. A development token, when
temporarily needed, belongs only in `runtime/tutor.env` and
`runtime/worker.env`, never in YAML.

Put the model API key only in `runtime/grader.env` under the
`OPENAI_API_KEY` variable. Enter the assigned value directly in an owner-only
editor; do not echo it through shell history.

Set `codex.enabled: true` in `runtime/config/config.yaml` after the grader is
configured. Compose injects the owner-only socket path into the worker through
a read-only bind; only the grader can modify the shared socket directory. The
grader receives no tutor config, state, GitHub key, dashboard secret, learner
checkout, or TCP port. The image pins Codex CLI and each request uses a
read-only sandbox, no approvals, and an ephemeral session. See the official
[Codex CLI documentation](https://developers.openai.com/codex/cli/) for current
authentication guidance.

Start the remote worker profile and reconcile the webhook:

```bash
docker compose --profile remote up -d
docker compose exec worker adaptive-tutor doctor
docker compose exec tutor adaptive-tutor webhook-setup
docker compose ps
```

### Provision ephemeral evaluators

Assignment publication creates a signed envelope under
`runtime/state/trusted-evaluators/spool` before writing the learner branch. The
runner autoscaler or other trusted provisioner must derive the assignment ID
and exact branch/commit from the bounded protected-workflow run title, then
stage that identity before registering a one-job runner. For a protected runner
staging directory:

```bash
install -d -m 0700 runtime/runner-staging/trusted
docker compose --profile remote run --rm --no-deps \
  --volume "$(pwd)/runtime/runner-staging:/runner/temp" \
  worker stage-evaluator A-0001 \
  --run-id 123456789 \
  --branch assignment/0001-bounded-work-queue \
  --commit-sha 0123456789abcdef0123456789abcdef01234567 \
  --output /runner/temp/trusted/assignment-bundle.json \
  --verification-key-output /runner/temp/trusted/evaluator-signing.pub
```

The destination directory and files must belong to the eventual runner user
with modes `0700` and `0600`. Transfer this protected directory through the
provisioner's authenticated channel, register the runner only after staging
succeeds, allow one job, and destroy the runner plus its temporary storage
afterward. Never place either staged file in the workspace repository, runner
image, cache, logs, or an Actions artifact.

The ephemeral runner image must provide Bubblewrap (`bwrap`) with user, PID,
network, IPC, and UTS namespace support. Install the locked `adaptive-tutor`
package, which includes pytest-xdist, before accepting work. The protected
workflow runs `command -v bwrap` and `bwrap --version` before consuming the
envelope; a missing isolation runtime is an infrastructure failure. Validate
namespace support in the image build, keep unprivileged user namespaces enabled
where Bubblewrap requires them, and never fall back to unsandboxed pytest.

### Lifecycle commands

```bash
# Start
docker compose --profile remote up -d

# Stop without deleting state
docker compose --profile remote stop

# Restart
docker compose --profile remote restart

# Status and readiness
docker compose ps
docker compose exec tutor adaptive-tutor status

# Logs
docker compose logs --since=30m tutor worker grader
docker compose logs --follow worker grader
```

Never use `docker compose down --volumes` as an operational shortcut. The
current deployment uses owner-only bind mounts, but volume-deleting habits make
future storage changes dangerous.

## Native systemd

### Install

Create separate state and model trust-domain accounts plus a socket-only group.
Do not add `adaptive-tutor` to the group in `/etc/group`; only the worker unit
receives it through `SupplementaryGroups=`:

```bash
sudo groupadd --system adaptive-tutor-grader-socket
sudo useradd --system --home-dir /var/lib/adaptive-tutor \
  --create-home --user-group --shell /usr/sbin/nologin adaptive-tutor
sudo useradd --system --home-dir /var/lib/adaptive-tutor-grader \
  --create-home --user-group --groups adaptive-tutor-grader-socket \
  --shell /usr/sbin/nologin adaptive-tutor-grader
sudo install -d -m 0700 -o adaptive-tutor -g adaptive-tutor \
  /etc/adaptive-tutor /var/lib/adaptive-tutor
sudo install -d -m 0700 -o adaptive-tutor-grader -g adaptive-tutor-grader \
  /var/lib/adaptive-tutor-grader /var/lib/adaptive-tutor-grader/codex
sudo install -d -m 0700 -o root -g root /etc/adaptive-tutor-grader
sudo python3 -m venv /opt/adaptive-tutor
sudo /opt/adaptive-tutor/bin/pip install /path/to/adaptive_tutor-release.whl
```

Install Codex CLI using the current
[official Codex CLI instructions](https://developers.openai.com/codex/cli/),
then make its executable available to `adaptive-tutor-grader`. Initialize the
application as the state-owning account:

```bash
sudo -u adaptive-tutor /opt/adaptive-tutor/bin/adaptive-tutor \
  --config /etc/adaptive-tutor/config.yaml init \
  --data-dir /var/lib/adaptive-tutor
sudo -u adaptive-tutor /opt/adaptive-tutor/bin/adaptive-tutor \
  --config /etc/adaptive-tutor/config.yaml doctor --offline
```

Configure the GitHub App in `/etc/adaptive-tutor/config.yaml` and set
`codex.enabled: true`. Put dashboard and webhook variables in
`/etc/adaptive-tutor/tutor.env`, worker-only GitHub variables in
`/etc/adaptive-tutor/worker.env`, and the model key only in the root-owned
`/etc/adaptive-tutor-grader/grader.env`. The state account must not be able to
replace the grader environment file:

```bash
sudo chown adaptive-tutor:adaptive-tutor /etc/adaptive-tutor/*.env
sudo chmod 0600 /etc/adaptive-tutor/*.env
sudo chown root:root /etc/adaptive-tutor-grader/grader.env
sudo chmod 0600 /etc/adaptive-tutor-grader/grader.env
```

Install and enable the units:

```bash
sudo install -m 0644 deploy/systemd/adaptive-tutor.service \
  deploy/systemd/adaptive-tutor-worker.service \
  deploy/systemd/adaptive-tutor-grader.service \
  deploy/systemd/adaptive-tutor-backup.service \
  deploy/systemd/adaptive-tutor-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adaptive-tutor.service adaptive-tutor-grader.service \
  adaptive-tutor-worker.service
sudo systemctl enable --now adaptive-tutor-backup.timer
```

### Lifecycle commands

```bash
# Start, stop, and restart
sudo systemctl start adaptive-tutor.service adaptive-tutor-grader.service \
  adaptive-tutor-worker.service
sudo systemctl stop adaptive-tutor-worker.service adaptive-tutor-grader.service \
  adaptive-tutor.service
sudo systemctl restart adaptive-tutor.service adaptive-tutor-grader.service \
  adaptive-tutor-worker.service

# Status, readiness, and logs
systemctl status adaptive-tutor.service adaptive-tutor-grader.service \
  adaptive-tutor-worker.service
curl --fail http://127.0.0.1:8765/readyz
journalctl -u adaptive-tutor.service -u adaptive-tutor-grader.service \
  -u adaptive-tutor-worker.service --since today
journalctl -u adaptive-tutor-worker.service --follow
```

The units use an empty capability set, strict filesystem protection,
owner-only state, private temporary directories, namespace restrictions, and
automatic restart after process failure. Tutor state and grader/Codex state
have different UIDs. Only worker and grader receive
`adaptive-tutor-grader-socket`; the tutor and backup cannot traverse the
runtime directory. The grader pre-binds the socket as
`adaptive-tutor-grader:adaptive-tutor-grader-socket` with mode `0660` inside a
`0750` directory, while the worker has no directory write permission. The
grader cannot see tutor state or configuration, and tutor processes cannot see
grader configuration or state. The backup timer is persistent, so a missed
backup runs after the next boot.

## Backup and restore

SQLite's online backup API produces a consistent snapshot while the service is
running:

```bash
adaptive-tutor --config /etc/adaptive-tutor/config.yaml backup
```

Compose operators can run the same command in the service container:

```bash
docker compose exec tutor adaptive-tutor backup
```

Backups land under the configured data directory's `backups/` directory with
mode `0600`. Copy them to encrypted off-host storage and test a restore at least
monthly. A backup that exists only beside the primary database is not disaster
recovery.

SQLite contains each complete assignment bundle, so the signed evaluator spool
is derived state. On a clean host, `stage-evaluator` creates a new owner-only key
and re-seals the requested database bundle before provisioning the next runner.
For an exact in-flight host snapshot, preserve `trusted-evaluators/` together;
never restore its envelopes without the matching `signing.key`.

Restore is intentionally explicit. Stop both writers, retain a copy of the
current database, and restore a verified snapshot:

```bash
sudo systemctl stop adaptive-tutor-worker.service adaptive-tutor-grader.service \
  adaptive-tutor.service
sudo -u adaptive-tutor /opt/adaptive-tutor/bin/adaptive-tutor \
  --config /etc/adaptive-tutor/config.yaml restore \
  /secure/path/tutor-backup.sqlite3 --yes
sudo -u adaptive-tutor /opt/adaptive-tutor/bin/adaptive-tutor \
  --config /etc/adaptive-tutor/config.yaml doctor --offline
sudo systemctl start adaptive-tutor.service adaptive-tutor-grader.service \
  adaptive-tutor-worker.service
```

For Compose, stop `worker`, `grader`, and `tutor`, place the snapshot in
`runtime/state/backups`, and run the restore through the tools profile before
starting services again.

## Upgrade and rollback

Every upgrade starts with an off-host backup. Then install or build the exact
release, run the migration/doctor check, and restart:

```bash
# Compose
docker compose exec tutor adaptive-tutor backup
docker compose build --pull
docker compose --profile remote up -d
docker compose exec worker adaptive-tutor doctor

# systemd
sudo systemctl stop adaptive-tutor-worker.service adaptive-tutor-grader.service \
  adaptive-tutor.service
sudo /opt/adaptive-tutor/bin/pip install --upgrade /path/to/new-release.whl
sudo -u adaptive-tutor /opt/adaptive-tutor/bin/adaptive-tutor \
  --config /etc/adaptive-tutor/config.yaml doctor --offline
sudo systemctl start adaptive-tutor.service adaptive-tutor-grader.service \
  adaptive-tutor-worker.service
```

When upgrading an older same-UID systemd installation to the split grader
identity, install the identity boundary and compatible binary/units as one
stopped operation:

```bash
sudo systemctl stop adaptive-tutor-worker.service adaptive-tutor-grader.service \
  adaptive-tutor.service
getent group adaptive-tutor-grader-socket >/dev/null || \
  sudo groupadd --system adaptive-tutor-grader-socket
id adaptive-tutor-grader >/dev/null 2>&1 || \
  sudo useradd --system --home-dir /var/lib/adaptive-tutor-grader \
    --user-group --groups adaptive-tutor-grader-socket \
    --shell /usr/sbin/nologin adaptive-tutor-grader
sudo usermod --append --groups adaptive-tutor-grader-socket adaptive-tutor-grader
sudo install -d -m 0700 -o root -g root /etc/adaptive-tutor-grader
sudo install -m 0600 -o root -g root /etc/adaptive-tutor/grader.env \
  /etc/adaptive-tutor-grader/grader.env
sudo rm -f /etc/adaptive-tutor/grader.env
sudo chown -R adaptive-tutor-grader:adaptive-tutor-grader \
  /var/lib/adaptive-tutor-grader
sudo /opt/adaptive-tutor/bin/pip install --upgrade /path/to/new-release.whl
sudo install -m 0644 deploy/systemd/adaptive-tutor*.service \
  deploy/systemd/adaptive-tutor-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start adaptive-tutor-grader.service adaptive-tutor.service
sudo systemctl start adaptive-tutor-worker.service
```

Rotate the model credential during this transition: the previous shared UID
could historically read it, and changing ownership cannot disprove prior
exposure. Verify the shared socket group owns nothing under
`/etc/adaptive-tutor-grader` or `/var/lib/adaptive-tutor-grader`.

Migrations are forward-only. Rollback therefore means restoring both the prior
application release and the pre-upgrade database snapshot. Do not run an older
binary against a database migrated by a newer release unless that release's
notes explicitly declare compatibility. Rolling back across the identity split
also requires stopping all units, restoring the old unit set, moving
`grader.env` back to `/etc/adaptive-tutor` with owner `adaptive-tutor`, and
returning grader-state ownership to that account before startup. Treat that
rollback as renewed credential exposure and rotate the model credential again
after returning to a separated release.

## Failure recovery

- **Host reboot or process crash:** Compose's restart policy and systemd's
  enabled units restart the service. Jobs remain in SQLite and expired leases
  become eligible for retry.
- **Network outage:** webhook deliveries can be retried by GitHub; queued jobs
  persist, use classified exponential retries, and retain dead-letter
  diagnostics after the retry limit.
- **Terminated grader:** the worker records a retryable transport/model failure
  and leaves learner state unchanged. Restart the isolated grader; the durable
  job retries after its lease expires.
- **Database corruption or host loss:** provision a clean host, install the same
  release, restore the newest tested off-host snapshot, run `doctor --offline`,
  then start the service and worker.
- **Lost webhook delivery:** run the reconciliation path after connectivity is
  restored; duplicate event deliveries are safe because delivery IDs and jobs
  are idempotent.

After any recovery, verify `/readyz`, `adaptive-tutor doctor`, worker logs,
pending/dead-letter jobs, webhook state, and one controlled private assignment
before declaring service restoration complete.
