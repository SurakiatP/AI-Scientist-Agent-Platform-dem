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
