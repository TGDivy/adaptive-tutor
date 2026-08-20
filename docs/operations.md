# Operations

Adaptive Tutor supports a hardened Docker Compose deployment and native
systemd services. Both paths keep SQLite on persistent storage and restart the
webhook service, durable worker, and isolated grader after a crash or reboot.
Learner code runs only in credential-free GitHub-hosted evaluation jobs, never
on the tutor host.

## Production Compose runbook

This is the recommended clean-server path. Use a dedicated Linux host with
Docker Engine and Compose v2, a public DNS name already pointing at it, inbound
TCP 80/443, a GitHub owner whose plan supports Actions and branch protection on
private repositories, and an OpenAI API key. Only amd64 and arm64 images are
supported by the pinned GitHub CLI layer.

### Install and initialize

Check out the exact public revision you intend to operate, then prepare owner-
only runtime storage and automatic Caddy TLS:

```bash
git clone https://github.com/TGDivy/adaptive-tutor.git
cd adaptive-tutor/deploy
./prepare-compose.sh --domain tutor.example.net
docker compose build
docker compose --profile tools run --rm initializer
```

`prepare-compose.sh` records the exact source commit in `deploy/.env`, creates
mode-0700 `runtime/config`, `runtime/state`, `runtime/codex`, and
`runtime/grader-run`, and creates mode-0600 service environment files.
Initialization writes private configuration, generated API/webhook secrets,
all SQLite migrations, and the bundled curriculum. It refuses to overwrite
existing state.

Put the model credential only in the grader environment file using an
owner-only editor:

```bash
${EDITOR:-vi} runtime/grader.env
chmod 0600 runtime/grader.env
```

Add one line named `OPENAI_API_KEY`. Do not put it in `tutor.env`,
`worker.env`, YAML, shell history, or GitHub. The grader receives this file but
cannot see tutor configuration, SQLite state, GitHub credentials, or a TCP
port. The image pins Codex CLI; every grading request is a fresh read-only,
no-approval process.

### Start HTTPS and the grader

```bash
docker compose --profile live --profile remote up -d tutor proxy grader
docker compose ps
curl --fail https://tutor.example.net/readyz
docker compose logs --tail=100 tutor proxy grader
```

Caddy obtains and renews the certificate. Compose also publishes the tutor at
`127.0.0.1:8765`; never change that mapping to a public bind. The dashboard is
still token-protected through Caddy. Read the token locally from
`runtime/state/secrets.env` and do not place it in URLs, proxy configuration,
screenshots, or support bundles.

### Authenticate the temporary bootstrap operator

The runtime image contains pinned `gh` and Git. Authenticate the one-shot
operator as the user or organization owner that may create the private
workspace and protect its default branch:

```bash
docker compose --profile remote --profile tools run --rm \
  --entrypoint gh operator auth login \
  --hostname github.com --git-protocol https --web
```

The login is stored under private tutor state only for setup. It is more
powerful than the steady-state App and must be removed after the final proof.

### Set the goal and run guided setup

Start setup with the public URL and the actual learning objective. Omit
`--github-owner` for the authenticated personal account; specify it for an
organization:

```bash
docker compose --profile remote --profile tools run --rm operator setup \
  --public-url https://tutor.example.net \
  --goal "Build reliable network services" \
  --github-owner YOUR_GITHUB_OWNER \
  --workspace-repo learning-workspace
```

An exit code of 2 means setup durably stopped for an operator action; it is not
lost progress. The goal is matched against the active curriculum's concept
names, domains, and `goal_terms`. An incompatible objective stops here so you
can load a matching private curriculum rather than receive unrelated work.

Setup first creates or verifies the one private workspace. Open
`https://tutor.example.net/setup`, sign in with the generated API token, and
follow **Create GitHub App**. Approve the manifest under the same owner and,
on the installation page, select only `learning-workspace`. The callback stores
the App key and webhook secret in owner-only state, validates exact App
permissions/events and one-repository scope, installs the protected evaluator
workflows/key, and waits for a signed `ping` or `installation` delivery.

After the browser returns to setup, restart the tutor once so its long-lived
assignment orchestrator uses the final App configuration:

```bash
docker compose restart tutor
```

Resume from the first incomplete step:

```bash
docker compose --profile remote --profile tools run --rm operator setup status
docker compose --profile remote --profile tools run --rm operator setup resume
```

The operator can reach the grader socket but cannot read its credential. Setup
runs a schema-valid Codex canary, dispatches a credential-free GitHub-hosted
probe, downloads and verifies its bound artifact, and creates the first private
assignment PR. A hosted run can take time; when status says it is scheduled or
running, wait for that Actions run and execute `setup resume` again. If the
webhook step is waiting, redeliver the App's recent `ping` from its GitHub
**Advanced** settings, then resume.

When setup reaches worker health, start the persistent worker and finish:

```bash
docker compose --profile remote up -d worker
docker compose --profile remote --profile tools run --rm operator setup resume
```

### Prove readiness and remove bootstrap access

Do not call the installation ready until this exits zero:

```bash
docker compose --profile remote --profile tools run --rm \
  operator doctor --live --strict
```

The live doctor revalidates all setup steps, public TLS, authenticated App
metadata and exact repository scope, signed webhook storage, protected
evaluator identity, isolated Codex canary, hosted credential-free artifact,
first assignment PR, and worker heartbeat. Then remove the temporary GitHub CLI
login and confirm steady-state services:

```bash
docker compose --profile remote --profile tools run --rm \
  --entrypoint gh operator auth logout --hostname github.com
docker compose --profile live --profile remote up -d
docker compose ps
```

Steady state now uses only the single-repository GitHub App. Preserve the setup
status and first PR URL in private operational records, not in this public
repository.

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

### GitHub-hosted evaluator controls

Remote deterministic checks use the protected
`.github/workflows/adaptive-tutor-evaluate.yml` workflow on GitHub-hosted
`ubuntu-24.04`. The protected default branch must also contain
`.adaptive-tutor/evaluator-signing.pub`. The tutor host retains the matching
private key and complete private assignment bundles; neither enters Actions.

Before publishing or dispatching, the orchestrator requires an
`evaluator_control_planes` record binding the immutable workspace repository
ID, workflow path/digest, exact public evaluator commit and kit digest, and
verification-key ID. The workflow verifies the signed learner-visible manifest
and public-test digests, then runs learner code under
`env -i` in a networkless Bubblewrap namespace. GitHub installs the isolation
runtime in each hosted job; there is no evaluator machine to operate on the
tutor server.

Guided setup installs and verifies those files, applies and reads back default-
branch protection, and creates the attested record. Do not insert or modify the
record manually. Assignment publication and dispatch fail closed if the
repository, workflow, key, evaluator revision, or digest later differs.

## Compose lifecycle commands

```bash
# Start
docker compose --profile live --profile remote up -d

# Stop without deleting state
docker compose --profile live --profile remote stop

# Restart
docker compose --profile live --profile remote restart

# Status and readiness
docker compose ps
docker compose exec tutor adaptive-tutor status
docker compose --profile remote --profile tools run --rm \
  operator doctor --live --strict

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

Install GitHub CLI and Codex CLI using their official packages, and make Codex
available to `adaptive-tutor-grader`. Initialize the application as the
state-owning account:

```bash
sudo -u adaptive-tutor /opt/adaptive-tutor/bin/adaptive-tutor \
  --config /etc/adaptive-tutor/config.yaml init \
  --data-dir /var/lib/adaptive-tutor
sudo -u adaptive-tutor /opt/adaptive-tutor/bin/adaptive-tutor \
  --config /etc/adaptive-tutor/config.yaml doctor --offline
```

Put the model key only in the root-owned
`/etc/adaptive-tutor-grader/grader.env`. The state account must not be able to
replace the grader environment file. Tutor/worker environment files are for
non-secret runtime overrides; generated API/webhook secrets remain in the
owner-only state secrets file:

```bash
sudo install -m 0600 -o adaptive-tutor -g adaptive-tutor /dev/null \
  /etc/adaptive-tutor/tutor.env
sudo install -m 0600 -o adaptive-tutor -g adaptive-tutor /dev/null \
  /etc/adaptive-tutor/worker.env
sudo install -m 0600 -o root -g root /dev/null \
  /etc/adaptive-tutor-grader/grader.env
sudoedit /etc/adaptive-tutor-grader/grader.env
```

Add only `OPENAI_API_KEY=...` to the grader file.

Install and enable the units:

```bash
sudo install -m 0644 deploy/systemd/adaptive-tutor.service \
  deploy/systemd/adaptive-tutor-worker.service \
  deploy/systemd/adaptive-tutor-grader.service \
  deploy/systemd/adaptive-tutor-backup.service \
  deploy/systemd/adaptive-tutor-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adaptive-tutor.service adaptive-tutor-grader.service
sudo systemctl enable --now adaptive-tutor-backup.timer
```

Terminate public TLS in a hardened reverse proxy that forwards the chosen
hostname to `127.0.0.1:8765`, preserves webhook headers and raw bodies, and
exposes `/readyz`. Authenticate the temporary GitHub bootstrap login:

```bash
sudo -u adaptive-tutor env HOME=/var/lib/adaptive-tutor \
  gh auth login --hostname github.com --git-protocol https --web
```

Run setup with the exact 40-character public source commit used to build the
installed wheel:

```bash
sudo -u adaptive-tutor env HOME=/var/lib/adaptive-tutor \
  ADAPTIVE_TUTOR_SOURCE_REVISION=YOUR_40_CHARACTER_COMMIT \
  /opt/adaptive-tutor/bin/adaptive-tutor \
  --config /etc/adaptive-tutor/config.yaml setup \
  --public-url https://tutor.example.net \
  --goal "Build reliable network services" \
  --github-owner YOUR_GITHUB_OWNER
```

Complete browser App approval at `/setup`, selecting only the created private
workspace, then restart `adaptive-tutor.service`. Resume setup with temporary
socket-group access so the trusted operator process can run the grader canary:

```bash
sudo systemctl restart adaptive-tutor.service
sudo -u adaptive-tutor -g adaptive-tutor-grader-socket env \
  HOME=/var/lib/adaptive-tutor \
  ADAPTIVE_TUTOR_CONFIG=/etc/adaptive-tutor/config.yaml \
  ADAPTIVE_TUTOR_GRADER_SOCKET=/run/adaptive-tutor-grader/grader.sock \
  ADAPTIVE_TUTOR_SOURCE_REVISION=YOUR_40_CHARACTER_COMMIT \
  /opt/adaptive-tutor/bin/adaptive-tutor setup resume
```

Repeat only when setup reports a completed external action. At worker health,
enable the worker, resume once more, and require the live doctor to pass:

```bash
sudo systemctl enable --now adaptive-tutor-worker.service
sudo -u adaptive-tutor -g adaptive-tutor-grader-socket env \
  HOME=/var/lib/adaptive-tutor \
  ADAPTIVE_TUTOR_CONFIG=/etc/adaptive-tutor/config.yaml \
  ADAPTIVE_TUTOR_GRADER_SOCKET=/run/adaptive-tutor-grader/grader.sock \
  /opt/adaptive-tutor/bin/adaptive-tutor setup resume
sudo -u adaptive-tutor -g adaptive-tutor-grader-socket env \
  HOME=/var/lib/adaptive-tutor \
  ADAPTIVE_TUTOR_CONFIG=/etc/adaptive-tutor/config.yaml \
  ADAPTIVE_TUTOR_GRADER_SOCKET=/run/adaptive-tutor-grader/grader.sock \
  /opt/adaptive-tutor/bin/adaptive-tutor doctor --live --strict
sudo -u adaptive-tutor env HOME=/var/lib/adaptive-tutor \
  gh auth logout --hostname github.com
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

An SQLite backup is not sufficient for a configured hosted evaluator. Back up
the owner-only `trusted-evaluators/` directory with the database, especially
`trusted-evaluators/signing.key`, and preserve its `0700`/`0600` permissions.
The signing key is not derivable from SQLite. The private bundle envelopes can
be reconstructed from SQLite only while the original key remains available;
published manifests and the protected workspace public key are anchored to
that key ID.

Losing `signing.key` without a backup makes existing trusted bundles and public
manifests unverifiable. Restore the matching key; never silently regenerate it
inside an existing installation. If no verified copy exists, retire the
affected workspace and tutor state and perform a fresh guided installation
with a new private workspace, retaining the old records as untrusted history.
Keep GitHub App keys, webhook/API secrets, grader credentials, configuration,
and evaluator signing state in the same encrypted disaster-recovery inventory,
with access separated by their trust domains.

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
./prepare-compose.sh
docker compose build --pull
docker compose --profile live --profile remote up -d
docker compose --profile remote --profile tools run --rm \
  operator doctor --live --strict

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
  release, restore the newest tested off-host database and evaluator signing
  state, run `doctor --offline`, then start the service and worker.
- **Lost evaluator signing key:** stop remote publication and dispatch, restore
  the matching key from a verified backup, and rerun the live doctor. Without a
  backup, preserve the old records offline and build a new installation and
  workspace; an unrelated replacement key cannot validate old manifests.
- **Lost webhook delivery:** run the reconciliation path after connectivity is
  restored; duplicate event deliveries are safe because delivery IDs and jobs
  are idempotent.

After any recovery, verify `/readyz`, `adaptive-tutor doctor`, worker logs,
pending/dead-letter jobs, webhook state, and one controlled private assignment
before declaring service restoration complete.
