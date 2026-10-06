# Local delivered web app

Build the existing frontend with `npm --prefix apps/web run build`. Copy `deploy/host.example.json` to a private path outside the repository, replace every placeholder with the reviewed local values, and set `web_dist_dir` to the absolute `apps/web/dist` path. The field is optional; omitting it preserves API-only hosts. The host serves this built UI and its SPA routes on the same loopback origin as the API. Vite remains for development only.

Use the already-owned Colima profile and its running PostgreSQL, MinIO, and engine services. Keep the delivered database (`DELIVEREDDB`), MinIO bucket (`DELIVEREDBUCKET`), secrets, host state, and scientific bundle separate from the W1 acceptance namespace. Retain the currently reviewed image pins and engine identity. This runbook does not build images, create services, or claim the actual-service or full-177 acceptance gates.

Create the private secrets and state directories with mode `0700`; keep secret files out of the repository. Set provider and peer destinations to the exact reviewed local configuration. Start the app with the configured workspace Python:

```sh
/absolute/path/to/workspace/.venv/bin/python -m scientist.host --config /absolute/private/delivered-app/host.json
```

The host writes the one-time owner URL to `state/owner-bootstrap.url`. Open it without printing the fragment token by running this helper in the project virtualenv, with the private URL file as its argument:

```python
import sys
import webbrowser
from pathlib import Path

url = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
if not url.startswith("http://127.0.0.1:") or "#bootstrap=" not in url:
    raise SystemExit("Unexpected owner URL")
webbrowser.open(url)
```

The browser removes the bootstrap fragment before sending it to the local API. The fragment is one-time; if startup fails, restart the local app to get a new URL. Do not paste the URL into logs, terminals, screenshots, or messages.

## Encrypted recovery set and isolated restore drill

`scientist.deployment_backup.create_recovery_set` captures PostgreSQL and every row in `stored_objects` as one recovery set. `restore_recovery_set` accepts only that closed manifest format, authenticates and decrypts every file before changing the target, restores the database, copies the verified immutable objects, and checks the restored object registry and bytes. The encrypted database dump and each object file use separate AES-GCM keys derived from a dedicated operator-managed 32-byte backup key. The manifest is authenticated. The master key used by `SCIENTIST_MASTER_KEY_FILE` is never included.

Run this only against the local deployment profile. Stop the host first so new API submissions stop; each operation also acquires the host's `scientist.host` PostgreSQL advisory lock and fails while the host or a runtime executor is active. Reconcile running/stopping work and unfinished uploads first. Queued work, consumed and reserved usage, unknown operations, and pending owner decisions remain in the database dump; restore never resends remote operations.

Provision a distinct backup key file outside the repository with exactly 32 random bytes and mode `0600`. Keep it separately from the recovery set. The recovery-set parent directory must be owned by the operator with mode `0700`, and the destination must not already exist. Use reviewed absolute `pg_dump` and `pg_restore` paths. PostgreSQL access follows the configured passwordless local host connection through libpq environment settings; credentials are never added to command arguments. The source object-store bucket must already exist.

For an opt-in restore acceptance drill, provision a fresh local PostgreSQL database and an existing empty MinIO bucket under different names from the source database and bucket. Keep the application host stopped throughout the drill. Call the API with those fresh target settings and the same private backup-key file:

```python
from pathlib import Path

from scientist.deployment_backup import create_recovery_set, restore_recovery_set

create_recovery_set(
    source_database_url,
    bucket=source_bucket,
    s3_client=source_s3_client,
    destination=Path("/absolute/private/backup/2026-10-06"),
    key_file=Path("/absolute/private/backup.key"),
    pg_dump_path=Path("/absolute/reviewed/bin/pg_dump"),
)
restore_recovery_set(
    fresh_target_database_url,
    target_bucket=fresh_target_bucket,
    s3_client=fresh_target_s3_client,
    recovery_set=Path("/absolute/private/backup/2026-10-06"),
    key_file=Path("/absolute/private/backup.key"),
    pg_restore_path=Path("/absolute/reviewed/bin/pg_restore"),
)
```

Treat a successful return as the restore gate, then record a private acceptance report confirming preserved run usage/reservations, unknown operations, pending owner decisions, access grants, current migration checksums, and the target bucket's verified object bytes. Provision the same reviewed runtime/profile assets and the existing application master key separately before starting the restored host. If restore fails at any point, keep the host stopped and rebuild the fresh target before retrying; do not dispatch from a partially restored target. This drill must not use a W1 acceptance namespace or the live delivered database/bucket.
