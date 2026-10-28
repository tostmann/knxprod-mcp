# knxprod-mcp: KNX product data for agents

## What this server is

- It answers questions about KNX application programs (com objects, parameters,
  memory layout) from the manufacturer product files (`.knxprod`) the user put
  into its product folder. It ships no manufacturer data of its own.
- It is read-only and never talks to a KNX bus. Reading from or writing to a
  device is the job of a bus tool, for example a busware KNX stick
  (`knx-prop-read`, `knx-mem-read`).
- The product files belong to their manufacturers. Use the answers to
  understand and configure the user's own devices; respect the manufacturers'
  terms and do not republish the data.

## The key: the ApplicationId

A device names its application program by the ApplicationId in interface
object 3, PID 13: 5 bytes = manufacturer (2) + application number (2) +
version (1), written as 10 hex digits, e.g. `FFF0123456`. That is `app_id` in
every tool.

- Read it from the device with a bus tool, e.g.
  `knx-prop-read {"device": "1.1.5", "object": 3, "pid": 13}`.
- Without device access: `prod-applications` with an order number or words of
  the product name. One program often serves several products (2-, 4-, 8-fold
  variants), so the program does not tell which hardware variant is installed,
  and only the device tells which version runs on it.

## Workflow: one tool call per step

1. `prod-application {"app_id": "FFF0123456"}`: program, products, memory
   segments, table locations, byte order, load procedure, modules.
2. `prod-comobjects {"app_id": "FFF0123456", "numbers": [16]}`: take the
   numbers from the device's group-address and association tables (bus tool).
3. `prod-parameters {"app_id": "FFF0123456", "query": "running time"}` or
   `{"app_id": "FFF0123456", "address": "0x4A0A"}`: where a setting lives.
4. Read those bytes on the device with a bus tool (e.g. `knx-mem-read`).
5. `prod-decode {"app_id": "FFF0123456", "address": "0x4A0A", "data": "<hex>"}`:
   the values, with the text of enumerated values.

## Com objects: pick the right variant

- One object number can have several variants (ComObjectRef): switching a
  channel from blind to switch mode changes its objects. Pick the variant whose
  `size_bits` equals the size the device reports for that object.
- Several variants of that size: the channel's configured mode decides. `when`
  names the mode parameter; read it from device memory and decode it.
- A name names the SLOT in the program, not what this installation uses it for
  (in switch mode a "slat position" slot may switch a pump). Do not present a
  slot name as the function without checking the configuration.

## Parameters

- `address` = segment base + `offset`. `bit_offset` counts from the MSB of the
  first byte; `extract` gives the bytes to read, the shift and the mask.
- A program with a relative segment has offsets but no addresses: pass
  `segment` and use the offset inside it as `address` (`prod-parameters`,
  `prod-decode`). Where a device places that segment is not in the file.
- `union` members share bytes. `when` says which channel mode each belongs to;
  only the member of the configured mode is meaningful. `ui: false` marks a
  parameter ETS never shows.
- Modules: many programs define a channel once (an ETS module) and instantiate
  it per channel. Every instance is listed on its own, with its own address or
  object number and `module` = the instance, e.g. "Channel B"; its ids start
  with the instance id (`MD-1_M-2_P-12`, repetitions add `_MI-3`). Filter
  with `module` (label or id).
  Alternative modules for one channel (switch / dimmer) share memory and
  object numbers; `when` says which one the channel mode selects.
  `prod-application` lists the module definitions.

## Byte order

Multi-byte values follow the byte order the program declares (`byte_order`).
A `byte_order_warning` means the manufacturer's memory template disagrees:
check multi-byte values on the device before relying on them and never write
them unverified.

## Writing is not this server's job

Changing a parameter means writing device memory in a load session with a bus
tool, following that tool's documented procedure. Read the current bytes
first, change only the parameter's bits, use the tool's dry run, then write.
Never write to a guessed address or for a guessed program version.

## Pagination and size

Every reply stays at or below 12 000 bytes. Lists take `offset` and `limit`
and return `total` and `next_offset` (null at the end). Ask narrow questions
(numbers, address, query, module) instead of paging through everything.

## Errors

- `unknown_app_id`: no product file with that program is indexed. Ask the user
  to add the manufacturer's `.knxprod` (copy it into the product folder or
  upload it). `candidates` are other versions of the program: their layout can
  differ, so do not answer from them in place of the device's own version.
- `invalid_argument`: the message says what to fix.
- When the device's own answer disagrees with the file, the device wins.

## Examples (synthetic ids)

What does object 16 of device 1.1.5 do?

1. Bus tool: object 3, PID 13 of 1.1.5 reads `FF F0 12 34 56`.
2. `prod-comobjects {"app_id": "FFF0123456", "numbers": [16]}`
3. The device reports object 16 as 1 bit: take the 1-bit variant. If two
   remain, read the mode parameter its `when` names and decode it.

What is the running time set to on that device?

1. `prod-parameters {"app_id": "FFF0123456", "query": "running time"}` gives
   address `0x4A0A`, `size_bits` 16.
2. Bus tool: read 2 bytes at 0x4A0A, e.g. `01 2C`.
3. `prod-decode {"app_id": "FFF0123456", "address": "0x4A0A", "data": "012C"}`
   gives 300.
