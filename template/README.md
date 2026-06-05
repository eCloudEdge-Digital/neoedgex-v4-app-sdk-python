# Python Template App

This template can be built into a container image and deployed to NeoFlow.

## Build

Use the SDK repository root as the Docker build context.

### Local SDK Source

To test the current local SDK checkout, build with `template/Dockerfile.local`. This image installs the SDK from the local build context:

```bash
docker build -f template/Dockerfile.local -t neoedgex-python-template:local .
```

### Published SDK

To build like an external app that installs the SDK as a published dependency, build with `template/Dockerfile.github`. This image resolves the SDK from the package index exactly as declared in `template/pyproject.toml`, so no credentials are required:

```bash
docker build -f template/Dockerfile.github -t neoedgex-python-template:github .
```

To install a specific SDK version, pin the `neoedgex-v4-app-sdk-python` dependency in `template/pyproject.toml`; the build picks it up automatically.

Both Dockerfiles use multi-stage builds. The final image installs only built wheels and the production entrypoint, so it does not retain the local SDK source tree or build tooling.

## Run Mock NeoEdgeX Locally

To run the template in mock mode directly from this repository:

```bash
cd /path/to/neoedgex-v4-app-sdk-python
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip pytest
export PYTHONPATH=src:template/src
export HTTP_ENDPOINT=http://127.0.0.1:8080/ingest
.venv/bin/python template/cmd/mock_neoedgex/main.py
```

This command:

- loads `template/cmd/mock_neoedgex/mock-config.json`
- injects mock input messages into the template app
- prints mock publish payloads to stdout

If you do not have a local HTTP endpoint yet, start a simple test server first:

```bash
python3 - <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        print("POST", self.path, body.decode())
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
PY
```

## NeoFlow Deployment

When deploying in NeoFlow, point the node image to the built image and keep the default production command. The container starts:

```bash
python /opt/template/cmd/general/main.py
```

If your app forwards data to an external HTTP service, set `HTTP_ENDPOINT` in the node environment variables.
