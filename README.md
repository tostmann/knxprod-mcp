# knxprod-mcp

An MCP server that answers questions about KNX devices from manufacturer product
files (`.knxprod`) that you obtained yourself. It reads the application programs in
those files and serves read-only MCP tools: which product an ApplicationId belongs
to, its com objects with all their variants, and its parameters with their memory
addresses, bit positions, value types and defaults.

This project ships no manufacturer data. You supply the product files and the
server indexes them. It is a single Python file that uses the standard library
only and is meant to run as a container in a private network. It announces itself
on the LAN via mDNS/DNS-SD, so clients can find it without a URL.

## Quick start

1. **Put your `.knxprod` files into a folder.** You can also use zip archives
   that contain a `.knxprod`. Subfolders are fine.

   ```sh
   mkdir -p knxprod knxprod-index
   cp ~/Downloads/*.knxprod knxprod/
   ```

2. **Start the container.** With the `docker-compose.yml` from this repository
   next to the two folders (it uses the published image
   `tostmann/knxprod-mcp`, for linux/amd64 and linux/arm64):

   ```sh
   docker compose up -d
   ```

   The Compose file uses host networking (Linux), which the discovery needs. The
   server then listens on the host's LAN address only (`KNXPROD_BIND=auto`). On
   Docker Desktop, switch to port mapping as described in `docker-compose.yml`.

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
   Clients that browse for the service find it by themselves, for example a
   busware KNX stick (see [Discovery](#discovery)). With Claude Code:

   ```sh
   claude mcp add --transport http knxprod http://<host>:8095/mcp
   ```

   An agent learns how to use the tools from the server itself: it serves a
   skill as the MCP resource `skill://knxprod-mcp/SKILL.md` (source:
   [`skill/SKILL.md`](skill/SKILL.md)), and its `initialize` instructions say to
   read that first.

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
docker run -d --name knxprod-mcp --network host -e KNXPROD_BIND=auto \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/knxprod:/knxprod" -v "$PWD/knxprod-index:/data" \
  --restart unless-stopped tostmann/knxprod-mcp
```

To build the image yourself: `docker build -t knxprod-mcp .` in a checkout.

With port mapping instead of host networking, discovery is not available:
replace `--network host -e KNXPROD_BIND=auto` with `-p 8095:8095`.

## Discovery

The server announces itself with mDNS/DNS-SD as service type `_knxprod-mcp._tcp`.
The instance name is `knxprod-mcp on <hostname>` and the TXT record says
`path=/mcp`. A client that browses for this type gets host, port and path and
needs no URL. Any DNS-SD browser shows it, for example
`avahi-browse -rt _knxprod-mcp._tcp`.

- In Docker it needs host networking (`network_mode: host`, Linux only). That
  is the default in `docker-compose.yml`.
- With port mapping or on Docker Desktop the container only sees internal
  addresses. The server then logs a warning and skips the announcement; enter
  `http://<host>:8095/mcp` in your client instead.
- The server shares UDP port 5353 with other mDNS responders such as Avahi. If
  the instance name is already taken on the network it adds ` (2)`, ` (3)`, …
  When it stops cleanly it withdraws the announcement.
- Switch it off with `--no-mdns` or `KNXPROD_MDNS=0`. `KNXPROD_MDNS_NAME`,
  `KNXPROD_MDNS_HOSTNAME` and `KNXPROD_MDNS_ADDRESS` set the announced instance
  name, host name and IPv4 address.

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
  rebuilt from the product folder on the next start. An index written by a
  version with another record format is rebuilt on start as well.
- A product folder mounted read-only works. Uploads are then refused.
- Unpacked product folders are not read. Keep the `.knxprod` (or zip) file.

## Tools

| tool | answers |
|---|---|
| `prod-applications` | which application programs are indexed (filter by manufacturer, words, order number) |
| `prod-application` | one program by ApplicationId: products, mask, memory segments, table locations, byte order, load procedure |
| `prod-comobjects` | com objects with all ComObjectRef variants: size, flags (C R W T U I), the ETS condition that shows each variant |
| `prod-parameters` | parameters with absolute address, bit offset (from the MSB), size, value type, default; filter by words, address or module instance |
| `prod-decode` | turns raw bytes read from device memory into parameter values |

A device identifies its application program with a 5-byte ApplicationId
(manufacturer, application number, version), for example `FFF0123456`. Use the
ApplicationId your device reports as `app_id`.

Some things to keep in mind:

- Com-object and parameter names label slots of the application program, not
  the function in an installation. What a channel really does depends on its
  configured mode.
- One com-object number can have several variants. Pick the variant whose
  `size_bits` matches what the device reports. When several variants have the
  same size, the channel's configured mode decides: read that parameter from
  the device and decode it with `prod-decode`.
- `when` lists the ETS conditions under which a variant or parameter applies,
  usually the channel mode. When ETS shows an item in several places, `when`
  keeps what all of them share and `when_any` lists the alternatives, one of
  which must hold as well. Places where it is hidden (`Access="None"`) do not
  count; an item ETS never shows gets `ui: false`. Conditions are complete,
  outermost first; `when_more` marks a chain longer than twelve.
- A parameter's `default`, `access` and `text` are those of the refs ETS shows.
  When channel modes give it different defaults, `variants` lists each ref with
  its default and conditions, and the parameter has no single `default`.
- Floats (DPT 9 and IEEE-754) and colours are sized from their encoding and
  decoded; `prod-decode` lists what it cannot decode in `skipped`.
- Products are linked through the catalog (`Catalog.xml`) where the product
  file has one, otherwise through the hardware.
- Multi-byte parameter values follow the byte order the program declares. The
  indexer compares that declaration with the defaults in the manufacturer's
  memory template and adds a `byte_order_warning` when they disagree.
- Many programs define a channel once as an ETS module and instantiate it per
  channel. Each instance is listed on its own: its parameters get their own
  addresses, its com objects their own numbers, and `module` names the instance
  (for example `Channel B`). Repetitions (`Repeat`, allocators) are expanded the
  same way. What cannot be resolved is counted in `counts.module_unsupported`.
- Programs that keep their parameters in a relative segment have offsets but no
  absolute addresses. Pass `segment` to `prod-parameters` and `prod-decode` and
  give the offset within that segment as `address`.

Every `tools/call` response stays at or below 12 000 bytes, so small clients
that forward replies can handle it. Long lists are paginated (`offset`/`limit`
in, `total`/`next_offset` out). A reply that had to leave detail out says so in
`detail`, and an object with more variants than one reply holds comes in parts
(`variants_next_offset` out, `variant_offset` in).

## Configuration

Every option can also be set as an environment variable; a command-line flag
wins over the environment.

| flag | environment | default | meaning |
|---|---|---|---|
| `--products` | `KNXPROD_DIR` | `/knxprod` | product folder |
| `--data` | `KNXPROD_DATA` | `/data` | index folder |
| `--bind` | `KNXPROD_BIND` | `127.0.0.1` | listen address; `auto` = this host's LAN address (the image default is `0.0.0.0`) |
| `--port` | `KNXPROD_PORT` | `8095` | listen port |
| `--allowed-host` | `KNXPROD_ALLOWED_HOSTS` | – | extra host names clients may use (comma separated) |
| `--upload-origin` | `KNXPROD_UPLOAD_ORIGINS` | private-network rule | browser origins allowed to upload; `none` turns browser uploads off |
| `--scan-interval` | `KNXPROD_SCAN_INTERVAL` | `10` | seconds between folder scans; `0` scans only at start |
| `--zip-max-mb` | `KNXPROD_ZIP_MAX_MB` | `4096` | largest uncompressed product data one file may hold (a full manufacturer database can exceed 1 GB) |
| `--allow-any-bind` | `KNXPROD_ALLOW_ANY_BIND=1` | off | allow listening on `0.0.0.0` / `::` |
| `--no-mdns` | `KNXPROD_MDNS=0` | on | do not announce the service via mDNS/DNS-SD |
| `--mdns-name` | `KNXPROD_MDNS_NAME` | `knxprod-mcp on <hostname>` | DNS-SD instance name |
| `--mdns-hostname` | `KNXPROD_MDNS_HOSTNAME` | this host's name | announced as `<name>.local` |
| `--mdns-address` | `KNXPROD_MDNS_ADDRESS` | listen or LAN address | IPv4 address(es) to announce (comma separated) |

Without Docker:

```sh
python3 knxprod_mcp.py serve --products ./knxprod --data ./knxprod-index --bind auto
python3 knxprod_mcp.py sync  --products ./knxprod --data ./knxprod-index   # index once, then exit
python3 knxprod_mcp.py health --data ./knxprod-index                       # 0 if the server is healthy
```

## Endpoints

- `POST /mcp`: MCP over Streamable HTTP. It is stateless (no session id) and
  answers with JSON only (no SSE). Protocol versions 2025-06-18, 2025-03-26 and
  2024-11-05 are supported. Besides the tools it serves one resource, the skill
  `skill://knxprod-mcp/SKILL.md`.
- `POST /upload?name=<file>`: the raw file as request body, at most 512 MB.
  Zip archives are checked for path traversal, member count, nesting depth,
  how far each member expands (more than 200:1 is refused) and how much product
  data they hold uncompressed (`KNXPROD_ZIP_MAX_MB`). Plugins and pictures in a
  product file are never unpacked and do not count.
- `GET /healthz`: liveness, index counts and the mDNS state. The container's
  health check (`knxprod_mcp.py health`) uses it.

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
- The mDNS announcement tells every device on the LAN that the service exists.
  Switch it off (`KNXPROD_MDNS=0`) where that is not wanted.
- The container runs as a non-root user. The product folder only needs to be
  writable if you want uploads.

## Tests

```sh
python3 tests/test_knxprod_mcp.py
```

The tests use a synthetic fixture with a fictitious manufacturer and send no
network traffic. To also check a real product file you own, set
`KNXPROD_TEST_REAL_DATA=/path/to/file.knxprod`. Optionally set
`KNXPROD_TEST_APP_ID` and `KNXPROD_TEST_OBJECTS` as well. To check the module
instances of programs in such a file, set `KNXPROD_TEST_MODULE_DATA` to the file
and `KNXPROD_TEST_MODULE_APPS` to their ApplicationIds (comma separated). A
live mDNS check on the LAN runs with
`KNXPROD_TEST_MDNS_ADDRESS=<a LAN address of this host>`.

## License

The code is licensed under MIT, see [LICENSE](LICENSE). The license covers this
project's code only. Manufacturer product files are not part of this project and
keep their manufacturers' licenses. Check those terms before you use the files
here; some restrict their use to KNX-recognised tools.
