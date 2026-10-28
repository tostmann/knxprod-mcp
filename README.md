# knxprod-mcp

An MCP server that answers questions about KNX devices from manufacturer product
files (`.knxprod`) that you obtained yourself. It reads the application programs in
those files and serves read-only MCP tools: which product an ApplicationId belongs
to, its com objects with all their variants, and its parameters with their memory
addresses, bit positions, value types and defaults.

This project ships no manufacturer data. You supply the product files and the
server indexes them. It is a single Python file that uses the standard library
only and is meant to run as a container in a private network.

## Quick start

1. **Put your `.knxprod` files into a folder.** You can also use zip archives
   that contain a `.knxprod`. Subfolders are fine.

   ```sh
   mkdir -p knxprod knxprod-index
   cp ~/Downloads/*.knxprod knxprod/
   ```

2. **Start the container.** Run this from a checkout of this repository; the
   image is built locally.

   ```sh
   docker compose up -d
   ```

   The container runs as UID/GID 1000 by default. If your folders belong to
   another user, start it with `KNXPROD_UID=$(id -u) KNXPROD_GID=$(id -g)
   docker compose up -d`.

3. **It indexes them.** On start the server reads every product file once, then
   starts listening. A large manufacturer database takes some seconds. Later
   starts only read new or changed files.

   ```sh
   docker compose logs -f              # "indexed <file>: N application program(s) ..."
   curl http://localhost:8095/healthz  # {"ok":true,...,"apps":N,"files":M,...}
   ```

4. **Connect your MCP client** to `http://<host>:8095/mcp` (Streamable HTTP).
   For example with Claude Code:

   ```sh
   claude mcp add --transport http knxprod http://<host>:8095/mcp
   ```

5. **Add more files** by copying them into the folder: the server picks them up
   within `KNXPROD_SCAN_INTERVAL` seconds. You can also upload them:

   ```sh
   curl --data-binary @Vendor.knxprod -H "Content-Type: application/zip" \
        "http://<host>:8095/upload?name=Vendor.knxprod"
   ```

   Web pages in the private network can upload straight from the browser, for
   example the web page of a busware KNX stick.

The same without Compose:

```sh
docker build -t knxprod-mcp .
docker run -d --name knxprod-mcp -p 8095:8095 --user "$(id -u):$(id -g)" \
  -v "$PWD/knxprod:/knxprod" -v "$PWD/knxprod-index:/data" \
  --restart unless-stopped knxprod-mcp
```

## How the product folder is used

- The folder is the source of truth. The server scans it recursively for
  `.knxprod` and `.zip` files and skips hidden files and folders.
- New or changed files are indexed. When a file disappears, the application
  programs that came only from that file disappear with it. A file whose size
  and modification time are unchanged is not read again, so a rescan is cheap.
- The server never moves, renames or modifies files there. An upload is added as
  a new file. If the name is already taken by a different file, the upload gets
  a numbered suffix. If identical content is already indexed, the upload is not
  stored a second time.
- The index (distilled JSON, one sha256 per product file) lives in the data
  folder (`/data` in the container). You can delete it at any time; it is
  rebuilt from the product folder on the next start.
- A product folder mounted read-only works. Uploads are then refused.
- Unpacked product folders are not read. Keep the `.knxprod` (or zip) file.

## Tools

| tool | answers |
|---|---|
| `prod-applications` | which application programs are indexed (filter by manufacturer, words, order number) |
| `prod-application` | one program by ApplicationId: products, mask, memory segments, table locations, byte order, load procedure |
| `prod-comobjects` | com objects with all ComObjectRef variants: size, flags (C R W T U I), the ETS condition that shows each variant |
| `prod-parameters` | parameters with absolute address, bit offset (from the MSB), size, value type, default; filter by words or address |
| `prod-decode` | turns raw bytes read from device memory into parameter values |

A device identifies its application program with a 5-byte ApplicationId
(manufacturer, application number, version), for example `FFF0123456`. Use the
ApplicationId your device reports as `app_id`.

Some things to keep in mind:

- Com-object and parameter names label slots of the application program, not
  the function in an installation. What a channel really does depends on its
  configured mode.
- One com-object number can have several variants. Pick the variant whose
  `size_bits` matches what the device reports.
- Multi-byte parameter values follow the byte order the program declares. The
  indexer compares that declaration with the defaults in the manufacturer's
  memory template and adds a `byte_order_warning` when they disagree.

Every `tools/call` response stays at or below 12 000 bytes, so small clients
that forward replies can handle it. Long lists are paginated (`offset`/`limit`
in, `total`/`next_offset` out).

## Configuration

Every option can also be set as an environment variable; a command-line flag
wins over the environment.

| flag | environment | default | meaning |
|---|---|---|---|
| `--products` | `KNXPROD_DIR` | `/knxprod` | product folder |
| `--data` | `KNXPROD_DATA` | `/data` | index folder |
| `--bind` | `KNXPROD_BIND` | `127.0.0.1` | listen address (the container uses `0.0.0.0`) |
| `--port` | `KNXPROD_PORT` | `8095` | listen port |
| `--allowed-host` | `KNXPROD_ALLOWED_HOSTS` | – | extra host names clients may use (comma separated) |
| `--upload-origin` | `KNXPROD_UPLOAD_ORIGINS` | private-network rule | browser origins allowed to upload; `none` turns browser uploads off |
| `--scan-interval` | `KNXPROD_SCAN_INTERVAL` | `10` | seconds between folder scans; `0` scans only at start |
| `--allow-any-bind` | `KNXPROD_ALLOW_ANY_BIND=1` | off | allow listening on `0.0.0.0` / `::` |

Without Docker:

```sh
python3 knxprod_mcp.py serve --products ./knxprod --data ./knxprod-index --bind 192.168.1.10
python3 knxprod_mcp.py sync  --products ./knxprod --data ./knxprod-index   # index once, then exit
```

## Endpoints

- `POST /mcp`: MCP over Streamable HTTP. It is stateless (no session id) and
  answers with JSON only (no SSE). Protocol versions 2025-06-18, 2025-03-26 and
  2024-11-05 are supported.
- `POST /upload?name=<file>`: the raw file as request body, at most 64 MB. Zip
  archives are checked for path traversal, member count, nesting depth and
  uncompressed size.
- `GET /healthz`: liveness and index counts. The container's health check uses
  it.

## Security

- Use it in a private network only. There is **no authentication**. Do not
  expose it to the internet, not even behind a port forward.
- DNS rebinding guard: requests must name the server by IP address,
  `localhost`, a `.local` name or a name listed in `KNXPROD_ALLOWED_HOSTS`. Any
  other Host header is refused. Browser requests to `/mcp` must come from an
  origin that passes the same test.
- Browser uploads (CORS on `/upload` only) are accepted from these origins:
  private IPv4 addresses (RFC 1918), IPv6 ULA and link-local addresses,
  `localhost` and `*.local`. Use `KNXPROD_UPLOAD_ORIGINS` to narrow this.
  Command-line clients such as curl send no Origin header and are not affected.
- The container runs as a non-root user. The product folder only needs to be
  writable if you want uploads.

## Tests

```sh
python3 tests/test_knxprod_mcp.py
```

The tests use a synthetic fixture with a fictitious manufacturer. To also check
a real product file you own, set `KNXPROD_TEST_REAL_DATA=/path/to/file.knxprod`.
Optionally set `KNXPROD_TEST_APP_ID` and `KNXPROD_TEST_OBJECTS` as well.

## License

The code is licensed under MIT, see [LICENSE](LICENSE). The license covers this
project's code only. Manufacturer product files are not part of this project and
keep their manufacturers' licenses. Check those terms before you use the files
here; some restrict their use to KNX-recognised tools.
