#!/usr/bin/env python3
"""knxprod-mcp -- MCP server for KNX manufacturer product files (.knxprod).

Point it at a directory of .knxprod files (or zip archives that contain
them).  It distills every application program into a small index and serves
read-only MCP tools about com objects, parameters and memory layout over
Streamable HTTP.  Python standard library only.

    knxprod_mcp.py serve  [--products DIR] [--data DIR] [--bind ADDR|auto] [--port N] ...
    knxprod_mcp.py sync   [--products DIR] [--data DIR]
    knxprod_mcp.py health [--data DIR]          (exit 0 if the running server is healthy)

Every option has an environment fallback: KNXPROD_DIR, KNXPROD_DATA,
KNXPROD_BIND, KNXPROD_PORT, KNXPROD_ALLOWED_HOSTS, KNXPROD_UPLOAD_ORIGINS,
KNXPROD_SCAN_INTERVAL, KNXPROD_ALLOW_ANY_BIND, KNXPROD_MDNS,
KNXPROD_MDNS_NAME, KNXPROD_MDNS_HOSTNAME, KNXPROD_MDNS_ADDRESS.

The server announces itself on the LAN via mDNS/DNS-SD as _knxprod-mcp._tcp
(TXT path=/mcp) so that clients can find it without a URL.

The product directory is the source of truth.  On start the server syncs
the index with it (new or changed files are ingested, the application
programs of removed files are dropped), then serves, and re-scans it every
KNXPROD_SCAN_INTERVAL seconds.  It never moves, renames or modifies a file
there; uploads are added as new files.  The index is one SQLite database in
the data directory, DATA/index.sqlite (keep DATA on a local disk).  Tools
take an APPID: manufacturer (4) + application number (4) + version (2) as
upper-case hex, e.g. FFF0123456, the ApplicationId a device reports about
itself; where two programs report the same one, their full ids
(M-FFF0_A-1234-56-ABCD) tell them apart.

No manufacturer data ships with this program.  Product files are licensed
by their manufacturers; whoever runs this server uses them under those terms.

Parser traps this code is built around:

1. About half of the <Memory> elements sit under <Union> (switchable channel
   modes), not under <Parameter>.  Union members carry Offset/BitOffset
   relative to the union's own <Memory>.
2. BitOffset counts from the MSB of the byte.
3. A regular expression for Offset="..." also matches BitOffset="...".  The
   XML is therefore never parsed with regular expressions here.
4. ElementTree iterparse + el.clear() on every end event deletes children (and
   their attributes) before the enclosing record has been processed.  Only
   complete records are cleared.
5. Parameters and com objects inside a <ModuleDef> are templates: a <Memory>
   with BaseOffset and a <ComObject> with BaseNumber are placed by the value
   each <Module> instance passes for that argument (NumericArg Value, or the
   next value of an Allocator for each repetition of a <Repeat>).  Every
   instance is distilled into records of its own.
"""

import argparse
import array
import base64
import collections
import contextlib
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
import random
import re
import shutil
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import sys
import threading
import time
import traceback
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

VERSION = "0.6.0"
SCHEMA_VERSION = 10                # index database format; another value rebuilds the index
DB_NAME = "index.sqlite"
SERVER_NAME = "knxprod-mcp"

DEFAULT_DATA = "/data"
DEFAULT_PRODUCTS = "/knxprod"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8095
DEFAULT_SCAN_INTERVAL = 10.0
SETTLE_SECONDS = 2.0               # a product file must be this old before it is read
PRODUCT_EXTENSIONS = (".knxprod", ".zip")

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"

# Every tools/call HTTP response body stays at or below this many bytes.  The
# result text itself is therefore smaller still.  Some clients are small
# devices that forward the reply.
RESPONSE_BUDGET = 12000

MAX_MCP_BODY = 1 << 20
MAX_UPLOAD = 512 << 20
# Zip-bomb guards.  Only members the service reads count: a manufacturer
# database also carries plugins and pictures nobody unpacks.  Real product
# XML expands < 40:1 (largest seen 38:1); a bomb expands ~1000:1.  The
# largest real database seen reads 1 GB, so 512 MB refused it.
ZIP_MAX_TOTAL = 4096 << 20         # declared uncompressed bytes read, all nesting levels
ZIP_MAX_RATIO = 200                # declared uncompressed : compressed, per member read
ZIP_MAX_MEMBERS = 5000             # members, all nesting levels
ZIP_MAX_DEPTH = 3
MAX_CONDS = 12                     # ETS conditions kept per place, outermost first (when_more beyond)
MAX_ALTS = 8                       # distinct condition sets (UI places) kept per object/parameter
BYTE_ORDER_MIN_EVIDENCE = 3        # template defaults needed before a byte-order warning
MAX_REPEAT = 256                   # module instances expanded per <Repeat>

SLOT_NOTE = ("Names label slots of the application program, not what an "
             "installation uses them for; the channel mode decides.")
VARIANT_NOTE = ("Pick the variant whose size_bits matches the object size the "
                "device reports; 'when' is the ETS condition for a variant, and with "
                "'when_any' one of those must hold as well.")

# The skill an agent reads first: skill/SKILL.md next to this file (in the
# image /app/skill/SKILL.md), served as an MCP resource.
SKILL_URI = "skill://knxprod-mcp/SKILL.md"
SKILL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill", "SKILL.md")
SKILL_DESCRIPTION = ("Read this first: the ApplicationId key, the workflow from a device to "
                     "decoded parameter values, variants, modules, byte order, errors.")

INSTRUCTIONS = (
    "knxprod-mcp answers questions about KNX application programs (com objects, "
    "parameters, memory layout) from the manufacturer product files (.knxprod) its "
    "operator supplied. It is read-only and never talks to a KNX bus. START by reading "
    "the resource " + SKILL_URI + ": the key to a device, the workflow and the pitfalls."
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def _local(tag):
    """Tag name without namespace (the knxprod schema version varies)."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _norm(s):
    """Collapse whitespace (texts contain CR/LF); None stays None."""
    if s is None:
        return None
    return _WS_RE.sub(" ", s).strip()


def _int(s, default=None):
    if s is None:
        return default
    try:
        return int(str(s).strip())
    except ValueError:
        return default


def _num(s):
    """int if integral, float otherwise, None if missing or not a number."""
    if s is None:
        return None
    s = str(s).strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _esc_len(text):
    """Bytes `text` occupies inside a JSON string literal (UTF-8, escaped)."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path, data):
    """Write atomically: <path>...tmp, fsync, os.replace, fsync the directory."""
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(os.path.dirname(path) or ".")


def atomic_write_json(path, obj):
    atomic_write_bytes(path, _dumps(obj).encode("utf-8"))


def _load_json(path, default=None):
    try:
        with open(path, "rb") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


_KNX_APP_RE = re.compile(
    r"^M-([0-9A-F]{4})_A-([0-9A-F]{4})-([0-9A-F]{2})(?:-[0-9A-F]{4})?(?:-O([0-9A-F]{4}))?",
    re.I)
_HEX10_RE = re.compile(r"^[0-9A-F]{10}$")


def normalize_app_id(value):
    """'FFF0123456', 'FF F0 12 34 56', '0xfff0123456' or 'M-FFF0_A-1234-56-ABCD'
    -> 'FFF0123456'; None if it is none of these."""
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    m = _KNX_APP_RE.match(s)
    if m:
        return (m.group(1) + m.group(2) + m.group(3)).upper()
    s = s.upper()
    if s.startswith("0X"):
        s = s[2:]
    s = re.sub(r"[\s:._-]", "", s)
    return s if _HEX10_RE.match(s) else None


_KNX_PROGRAM_RE = re.compile(
    r"^M-[0-9A-F]{4}_A-[0-9A-F]{4}-[0-9A-F]{2}-[0-9A-F]{4}(?:-O[0-9A-F]{4})?$", re.I)


def normalize_program_id(value):
    """A program's full id ('M-FFF0_A-1234-56-ABCD', with the -Oxxxx of an OEM
    program) in upper case; None for anything else."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s.upper() if _KNX_PROGRAM_RE.match(s) else None


_FLAG_ATTRS = (("C", "CommunicationFlag"), ("R", "ReadFlag"), ("W", "WriteFlag"),
               ("T", "TransmitFlag"), ("U", "UpdateFlag"), ("I", "ReadOnInitFlag"))
_SIZE_RE = re.compile(r"^\s*(\d+)\s*(bit|bits|byte|bytes)\s*$", re.I)


def size_bits(object_size):
    """'1 Bit' -> 1, '2 Bytes' -> 16, anything else -> None."""
    m = _SIZE_RE.match(object_size or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if m.group(2).lower().startswith("bit") else n * 8


def flags_str(flags):
    """C R W T U I in this order, '-' where the flag is off."""
    out = []
    for letter, _ in _FLAG_ATTRS:
        v = (flags.get(letter) or "").lower()
        out.append(letter if v in ("enabled", "true", "1") else "-")
    return "".join(out)


def fmt_dpt(dpt):
    """'DPST-5-1' -> '5.001', 'DPT-9' -> '9.xxx'; several are comma separated."""
    out = []
    for tok in (dpt or "").split():
        m = re.match(r"^DPST-(\d+)-(\d+)$", tok)
        if m:
            out.append("%d.%03d" % (int(m.group(1)), int(m.group(2))))
            continue
        m = re.match(r"^DPT-(\d+)$", tok)
        out.append("%d.xxx" % int(m.group(1)) if m else tok)
    return ",".join(out) or None


def extract_bits(data, offset, bit_offset, size, byte_order="big"):
    """Field value: `size` bits starting `bit_offset` bits after the MSB of
    data[offset].  A field that spans several bytes is read as one integer in
    the application's parameter byte order; little-endian is only defined
    here for byte-aligned fields (bit_offset 0, whole bytes) -> else None."""
    nbytes = (bit_offset + size + 7) // 8
    if offset < 0 or offset + nbytes > len(data):
        return None
    if byte_order == "little" and nbytes > 1:
        if bit_offset or size % 8:
            return None
        return int.from_bytes(data[offset:offset + nbytes], "little")
    v = int.from_bytes(data[offset:offset + nbytes], "big")
    return (v >> (nbytes * 8 - bit_offset - size)) & ((1 << size) - 1)


def extract_rule(bit_offset, size, byte_order="big"):
    """How to get the value out of device memory (see extract_bits)."""
    nbytes = (bit_offset + size + 7) // 8
    if size > 32:
        return {"bytes": nbytes}
    if byte_order == "little" and nbytes > 1:
        if bit_offset or size % 8:
            return {"bytes": nbytes, "note": "little-endian bit field: not defined"}
        return {"bytes": nbytes, "order": "little", "shift": 0,
                "mask": "0x%X" % ((1 << size) - 1)}
    return {"bytes": nbytes, "shift": nbytes * 8 - bit_offset - size,
            "mask": "0x%X" % ((1 << size) - 1)}


# Sizes ETS leaves to the encoding: a TypeFloat or TypeColor carries no
# SizeInBit, and without a size such a parameter had no extract rule, was
# missing from prod-decode and from the address filter (review 2026-10-29).
_COLOR_BITS = {"RGB": 24, "HSV": 24}


def float_bits(encoding):
    """'DPT 9' -> 16 (2-byte KNX float), 'IEEE-754 Single' -> 32, '... Double' -> 64."""
    e = (encoding or "").lower()
    if "single" in e or re.search(r"dpt\s*14\b", e):
        return 32
    if "double" in e:
        return 64
    if re.search(r"dpt\s*9\b", e):
        return 16
    return None


def decode_float(chunk, byte_order="big"):
    """2 bytes: DPT 9 (0.01 * M * 2^E, M 12-bit two's complement), None for its
    invalid value 0x7FFF; 4/8 bytes: IEEE-754 single/double."""
    if len(chunk) == 2:
        raw = int.from_bytes(chunk, byte_order)
        if raw == 0x7FFF:
            return None
        m = (raw & 0x07FF) - (0x0800 if raw & 0x8000 else 0)
        return round(0.01 * m * (1 << ((raw >> 11) & 0x0F)), 2)
    fmt = {4: "f", 8: "d"}.get(len(chunk))
    if fmt is None:
        return None
    return struct.unpack(("<" if byte_order == "little" else ">") + fmt, chunk)[0]


def byte_order_of(declared):
    """'BigEndian' / 'LittleEndian' / None (not stated -> big, the KNX default)."""
    return "little" if (declared or "").lower().startswith("little") else "big"


def _intval(s):
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# translations
# ---------------------------------------------------------------------------

def _merge_langs(langs):
    """{lang: {refid: {attr: text}}} -> (english, german) maps; en-US / de-DE first."""
    def pick(prefix, preferred):
        codes = sorted((c for c in langs if c.lower().startswith(prefix)),
                       key=lambda c: (c != preferred, c))
        out = {}
        for code in codes:
            for refid, attrs in langs[code].items():
                d = out.setdefault(refid, {})
                for a, t in attrs.items():
                    d.setdefault(a, t)
        return out
    return pick("en", "en-US"), pick("de", "de-DE")


class _Texts:
    """English text first, German as text_de where it is cheap to have."""

    def __init__(self, en, de, default_lang):
        self.en = en
        self.de = de
        self.default_lang = (default_lang or "").lower()

    def get(self, elem_id, attr, raw, default_lang=None):
        dl = (default_lang or self.default_lang or "").lower()
        base = _norm(raw) or None
        en = de = None
        if elem_id:
            en = _norm((self.en.get(elem_id) or {}).get(attr)) or None
            de = _norm((self.de.get(elem_id) or {}).get(attr)) or None
        if en is None and dl.startswith("en"):
            en = base
        if de is None and dl.startswith("de"):
            de = base
        text = en or base or de or ""
        text_de = de if de and de != text else None
        return text, text_de


def _collect_translation(langs, lang, el):
    d = langs.setdefault(lang or "", {}).setdefault(el.get("RefId") or "", {})
    for t in el:
        if _local(t.tag) == "Translation":
            attr = t.get("AttributeName")
            if attr:
                d[attr] = t.get("Text") or ""


# ---------------------------------------------------------------------------
# XML parsing (streaming; see trap 4 in the module docstring)
# ---------------------------------------------------------------------------

class _RawApp:
    """Everything of one <ApplicationProgram>, still with full XML ids.  Each
    <ModuleDef> gets an instance of its own for the records it defines."""

    def __init__(self, attrs):
        self.attrs = attrs
        self.full_id = attrs.get("Id", "")
        self.segments = {}
        self.seg_data = {}
        self.types = {}
        self.params = []
        self.param_refs = []
        self.comobjects = []
        self.comobject_refs = []
        self.tables = {}
        self.options = {}
        self.ldprocs = []
        self.dynamic = {"co": {}, "pr": {}, "modules": [], "first_block": None}
        self.module_defs = {}           # ModuleDef Id -> _RawApp with its records
        self.args = {}                  # in a ModuleDef: Argument Id -> declaration
        self.allocators = {}            # Allocator Id -> {name, start, max}
        self.unsupported = collections.Counter()

    # -- Static/Code
    def _template(self, sid, el, size):
        """<Data> of a segment: the manufacturer's memory image, usually
        holding the parameter defaults."""
        for c in el:
            if _local(c.tag) == "Data" and c.text:
                try:
                    data = base64.b64decode(c.text)
                except ValueError:
                    data = None
                if data is not None:
                    self.seg_data[sid] = data[:size] if size else data

    def add_abs_segment(self, el):
        sid = el.get("Id")
        size = _int(el.get("Size"))
        self.segments[sid] = {"kind": "absolute", "address": _int(el.get("Address")),
                              "size": size}
        self._template(sid, el, size)

    def add_rel_segment(self, el):
        sid = el.get("Id")
        size = _int(el.get("Size"))
        self.segments[sid] = {"kind": "relative", "offset": _int(el.get("Offset")),
                              "size": size, "lsm": _int(el.get("LoadStateMachine"))}
        self._template(sid, el, size)

    # -- Static/ParameterTypes
    def add_type(self, el):
        t = {}
        for c in el:
            k = _local(c.tag)
            if k == "TypeNumber":
                t = {"kind": "number", "size_bits": _int(c.get("SizeInBit")),
                     "signed": (c.get("Type") or "").lower().startswith("signed"),
                     "min": _num(c.get("minInclusive")), "max": _num(c.get("maxInclusive"))}
            elif k == "TypeRestriction":
                vals = []
                for e in c:
                    if _local(e.tag) == "Enumeration":
                        v = e.get("Value")
                        b = None
                        if e.get("BinaryValue"):        # Base="BinaryValue": these bytes are stored
                            try:
                                b = base64.b64decode(e.get("BinaryValue")).hex().upper()
                            except ValueError:
                                b = None
                        vals.append({"id": e.get("Id"), "value": v if v is not None else b,
                                     "text": e.get("Text"), "binary": b})
                t = {"kind": "enum", "size_bits": _int(c.get("SizeInBit")), "base": c.get("Base"),
                     "values": vals}
            elif k == "TypeText":
                t = {"kind": "text", "size_bits": _int(c.get("SizeInBit"))}
            elif k == "TypeFloat":
                t = {"kind": "float",
                     "size_bits": _int(c.get("SizeInBit")) or float_bits(c.get("Encoding")),
                     "encoding": c.get("Encoding"),
                     "min": _num(c.get("minInclusive")), "max": _num(c.get("maxInclusive"))}
            elif k == "TypeColor":
                space = c.get("Space")
                t = {"kind": "color", "space": space,
                     "size_bits": _int(c.get("SizeInBit")) or _COLOR_BITS.get((space or "").upper())}
            elif k in ("TypeNone", "TypePicture"):
                t = {"kind": k[4:].lower(), "size_bits": 0}
            elif k == "TypeIPAddress":
                t = {"kind": "ip", "size_bits": 32}
            elif k.startswith("Type"):
                t = {"kind": k[4:].lower(), "size_bits": _int(c.get("SizeInBit"))}
            else:
                continue
            break
        self.types[el.get("Id")] = t

    # -- Static/Parameters
    @staticmethod
    def _location(m, add_offset, add_bits):
        """Byte offset and MSB-first bit offset of a <Memory>/<Property>,
        shifted by a union member's own Offset/BitOffset.  In a module
        definition, BaseOffset names the argument whose value is added to
        the byte offset of each instance."""
        total = ((_int(m.get("Offset"), 0) + add_offset) * 8
                 + _int(m.get("BitOffset"), 0) + add_bits)
        if _local(m.tag) == "Memory":
            loc = {"kind": "memory", "segment": m.get("CodeSegment"),
                   "offset": total // 8, "bit_offset": total % 8}
        else:
            loc = {"kind": "property", "offset": total // 8, "bit_offset": total % 8}
            for key, attr in (("object_index", "ObjectIndex"), ("object_type", "ObjectType"),
                              ("pid", "PropertyId"), ("occurrence", "Occurrence")):
                v = _int(m.get(attr))
                if v is not None:
                    loc[key] = v
        if m.get("BaseOffset"):
            loc["base"] = m.get("BaseOffset")
        return loc

    @staticmethod
    def _param_base(el):
        return {"id": el.get("Id"), "name": el.get("Name"), "text": el.get("Text"),
                "type": el.get("ParameterType"), "access": el.get("Access"),
                "default": el.get("Value"), "loc": None,
                "text_param": el.get("TextParameterRefId")}

    def add_parameter(self, el):
        p = self._param_base(el)
        for c in el:
            if _local(c.tag) in ("Memory", "Property"):
                p["loc"] = self._location(c, 0, 0)
        self.params.append(p)

    def add_union(self, el):
        storage = None
        for c in el:
            if _local(c.tag) in ("Memory", "Property"):
                storage = c
        usize = _int(el.get("SizeInBit"))
        for c in el:
            if _local(c.tag) != "Parameter":
                continue
            p = self._param_base(c)
            if storage is not None:
                p["loc"] = self._location(storage, _int(c.get("Offset"), 0),
                                          _int(c.get("BitOffset"), 0))
            p["union"] = {"size_bits": usize,
                          "default": (c.get("DefaultUnionParameter") or "").lower() in ("1", "true")}
            self.params.append(p)

    def add_parameter_ref(self, el):
        self.param_refs.append({"id": el.get("Id"), "ref": el.get("RefId"),
                                "text": el.get("Text"), "default": el.get("Value"),
                                "access": el.get("Access"),
                                "text_param": el.get("TextParameterRefId")})

    # -- Static/ComObjectTable (ComObjects in a module definition), ComObjectRefs
    def add_comobject(self, el):
        self.comobjects.append({
            "id": el.get("Id"), "number": _int(el.get("Number")), "name": el.get("Name"),
            "text": el.get("Text"), "function_text": el.get("FunctionText"),
            "size": el.get("ObjectSize"), "priority": el.get("Priority"),
            "flags": {k: el.get(a) for k, a in _FLAG_ATTRS}, "dpt": el.get("DatapointType"),
            "base_number": el.get("BaseNumber"), "text_param": el.get("TextParameterRefId")})

    def add_comobject_ref(self, el):
        self.comobject_refs.append({
            "id": el.get("Id"), "ref": el.get("RefId"), "text": el.get("Text"),
            "function_text": el.get("FunctionText"), "size": el.get("ObjectSize"),
            "priority": el.get("Priority"), "flags": {k: el.get(a) for k, a in _FLAG_ATTRS},
            "dpt": el.get("DatapointType"), "text_param": el.get("TextParameterRefId")})

    # -- Static/LoadProcedures
    def add_load_procedure(self, el):
        steps = [(_local(c.tag), dict(c.attrib)) for c in el if _local(c.tag).startswith("LdCtrl")]
        self.ldprocs.append((el.get("MergeId"), steps))

    # -- ModuleDef/Arguments, Static/Allocators
    def add_argument(self, el):
        self.args[el.get("Id")] = {"name": el.get("Name") or "",
                                   "text": (el.get("Type") or "").lower() == "text",
                                   "allocates": _int(el.get("Allocates"))}

    def add_allocator(self, el):
        self.allocators[el.get("Id")] = {"name": el.get("Name"), "start": _int(el.get("Start")),
                                         "max": _int(el.get("maxInclusive"))}


def _note_ref(table, refid, conds, block):
    """Count one place the ETS UI shows a reference.  Kept: the block of the
    first place and every distinct chain of conditions — a reference shown
    under two channel modes needs both (one chain over MAX_ALTS marks that
    there are more).  The whole chain: keeping only the innermost conditions
    dropped the outer ones, and those are usually the channel mode."""
    if not refid:
        return
    c = tuple(conds)
    r = table.get(refid)
    if r is None:
        table[refid] = [1, [c], block]
    else:
        r[0] += 1
        if c not in r[1] and len(r[1]) <= MAX_ALTS:
            r[1].append(c)


def _merge_entries(entries):
    """One UI entry for all references of one parameter: their places add up,
    and so do their condition sets.  None when the UI shows none of them."""
    out = None
    for e in entries:
        if out is None:
            out = [e[0], list(e[1]), e[2]]
            continue
        out[0] += e[0]
        for c in e[1]:
            if c not in out[1] and len(out[1]) <= MAX_ALTS:
                out[1].append(c)
    return out


def _uniq(items):
    out = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


def _when_fields(alts):
    """`when` and `when_any` from the condition lists of every place the ETS UI
    shows an item.  What all places share (usually the channel mode) stays in
    `when`; the rest goes to `when_any`, one of which must hold as well.  A
    place without further conditions makes `when` alone enough."""
    alts = _uniq(alts)
    if len(alts) <= 1:
        return {"when": alts[0]} if alts and alts[0] else {}
    common = [c for c in alts[0] if all(c in a for a in alts[1:])]
    o = {"when": common} if common else {}
    rest = _uniq([c for c in a if c not in common] for a in alts)
    if all(rest):
        o["when_any"] = rest[:MAX_ALTS]
        if len(rest) > MAX_ALTS:
            o["when_any_more"] = True
    return o


def walk_dynamic(dyn, in_module=False):
    """Record, per ComObjectRef/ParameterRef id, how often the ETS UI shows it,
    every distinct set of innermost conditions (choose/when) it is shown under
    and the block of its first occurrence; and every <Module> instance with its
    arguments, conditions
    and enclosing <Repeat>.  Iterative: the nesting can be deep.  Modules
    inside a module definition and repeats inside repeats are only counted."""
    co, pr, modules = {}, {}, []
    unsupported = collections.Counter()
    first_block = None
    # frame: [children, conditions, block, (choose param, tests) or None, repeat]
    stack = [[iter(list(dyn)), (), None, None, None]]
    while stack:
        frame = stack[-1]
        c = next(frame[0], None)
        if c is None:
            stack.pop()
            continue
        conds, block, choose, repeat = frame[1], frame[2], frame[3], frame[4]
        k = _local(c.tag)
        if choose is not None:              # iterating the <when>s of a <choose>
            if k != "when":
                continue
            pref, tests = choose
            test = c.get("test")
            is_default = (c.get("default") or "").lower() in ("true", "1")
            if test is not None and not is_default:
                cond = (pref, test, None)
            elif tests:
                cond = (pref, None, tests)  # default branch: none of the other tests
            else:
                cond = None
            stack.append([iter(list(c)), conds + ((cond,) if cond else ()), block, None, repeat])
        elif k == "choose":
            whens = [w for w in c if _local(w.tag) == "when"]
            tests = tuple(w.get("test") for w in whens
                          if w.get("test") is not None
                          and (w.get("default") or "").lower() not in ("true", "1"))
            stack.append([iter(whens), conds, block, (c.get("ParamRefId"), tests), repeat])
        elif k == "ComObjectRefRef":
            _note_ref(co, c.get("RefId"), conds, block)
        elif k == "ParameterRefRef":
            _note_ref(pr, c.get("RefId"), conds, block)
        elif k in ("Module", "Repeat") and in_module:
            unsupported["%s_in_module_def" % k.lower()] += 1
        elif k == "Repeat" and repeat is not None:
            unsupported["repeat_in_repeat"] += 1
        elif k == "Module":
            args = {}
            for a in c:
                ak = _local(a.tag)
                if ak == "NumericArg":
                    args[a.get("RefId")] = {"value": a.get("Value"),
                                            "allocator": a.get("AllocatorRefId")}
                elif ak == "TextArg":
                    args[a.get("RefId")] = {"value": a.get("Value"), "text_id": a.get("Id")}
            modules.append({"id": c.get("Id"), "ref": c.get("RefId"), "name": c.get("Name"),
                            "args": args, "conds": conds, "repeat": repeat})
        elif k == "Repeat":
            rep = {"id": c.get("Id"), "count": _int(c.get("Count")),
                   "pref": c.get("ParameterRefId")}
            stack.append([iter(list(c)), conds, block, None, rep])
        elif k in ("ParameterBlock", "Channel", "ChannelIndependentBlock"):
            nb = (k, c.get("Id"), c.get("Text"), c.get("ParamRefId"), c.get("Name"),
                  c.get("TextParameterRefId"))
            if first_block is None and block is None and c.get("Text"):
                first_block = nb
            stack.append([iter(list(c)), conds, nb, None, repeat])
        else:
            stack.append([iter(list(c)), conds, block, None, repeat])
    return {"co": co, "pr": pr, "modules": modules, "first_block": first_block,
            "unsupported": unsupported}


# elements whose children are complete records that were already processed
_APP_CONTAINERS = {"Code", "ParameterTypes", "Parameters", "ParameterRefs", "ComObjectTable",
                   "ComObjects", "ComObjectRefs", "LoadProcedures", "Static", "ModuleDefs",
                   "SubModuleDefs", "Arguments", "Allocators"}


def parse_application_xml(stream):
    """Stream-parse one application program XML.  Returns ([_RawApp], langs).

    A record (Parameter, Union, ComObject, ...) is processed at ITS end event,
    when all of its children are complete, and cleared only afterwards.  The
    <Parameter> children of a <Union> are deliberately NOT handled at their
    own end events: clearing them there would wipe Offset/BitOffset before
    the union is processed (trap 4).  Records inside a <ModuleDef> go to that
    definition; a definition nested inside another one is only counted."""
    apps = []
    langs = {}
    cur = None
    lang = None
    tags = []
    defs = []                       # open <ModuleDef>s: _RawApp, None when nested
    for event, el in ET.iterparse(stream, events=("start", "end")):
        tag = _local(el.tag)
        if event == "start":
            tags.append(tag)
            if tag == "ApplicationProgram":
                cur = _RawApp(dict(el.attrib))
                defs = []
            elif cur is not None and tag == "ModuleDef":
                if defs:
                    defs.append(None)
                    cur.unsupported["module_def_nested"] += 1
                else:
                    md = _RawApp(dict(el.attrib))
                    cur.module_defs[md.full_id] = md
                    defs.append(md)
            elif cur is not None and not defs and tag in (
                    "ComObjectTable", "AddressTable", "AssociationTable"):
                cur.tables[tag] = dict(el.attrib)
            elif cur is not None and not defs and tag == "Options":
                cur.options = dict(el.attrib)
            elif tag == "Language":
                lang = el.get("Identifier") or ""
            continue
        tags.pop()
        parent = tags[-1] if tags else ""
        if tag == "TranslationElement":
            _collect_translation(langs, lang, el)
            el.clear()
            continue
        if cur is None:
            if tag in ("TranslationUnit", "Language", "Languages", "ApplicationPrograms"):
                el.clear()
            continue
        if tag == "ModuleDef":
            defs.pop()
            el.clear()
            continue
        target = defs[-1] if defs else cur
        if target is None:                  # inside a nested definition
            continue
        if tag == "AbsoluteSegment" and parent == "Code":
            cur.add_abs_segment(el)
            el.clear()
        elif tag == "RelativeSegment" and parent == "Code":
            cur.add_rel_segment(el)
            el.clear()
        elif tag == "ParameterType" and parent == "ParameterTypes":
            cur.add_type(el)
            el.clear()
        elif tag == "Parameter" and parent == "Parameters":
            target.add_parameter(el)
            el.clear()
        elif tag == "Union" and parent == "Parameters":
            target.add_union(el)
            el.clear()
        elif tag == "ParameterRef" and parent == "ParameterRefs":
            target.add_parameter_ref(el)
            el.clear()
        elif tag == "ComObject" and parent in ("ComObjectTable", "ComObjects"):
            target.add_comobject(el)
            el.clear()
        elif tag == "ComObjectRef" and parent == "ComObjectRefs":
            target.add_comobject_ref(el)
            el.clear()
        elif tag == "Argument" and parent == "Arguments":
            target.add_argument(el)
            el.clear()
        elif tag == "Allocator" and parent == "Allocators":
            cur.add_allocator(el)
            el.clear()
        elif tag == "LoadProcedure" and parent == "LoadProcedures":
            cur.add_load_procedure(el)
            el.clear()
        elif tag == "Dynamic" and parent in ("ApplicationProgram", "ModuleDef"):
            dyn = walk_dynamic(el, in_module=target is not cur)
            cur.unsupported.update(dyn.pop("unsupported"))
            target.dynamic = dyn
            el.clear()
        elif tag in _APP_CONTAINERS:
            el.clear()
        elif tag == "ApplicationProgram":
            apps.append(cur)
            cur = None
            el.clear()
    return apps, langs


def parse_hardware_xml(stream):
    """Hardware.xml -> products, the programs of each hardware, and which
    program each Hardware2Program loads (for Catalog.xml)."""
    programs = {}
    products = {}
    h2p = {}
    langs = {}
    tags = []
    lang = None
    for event, el in ET.iterparse(stream, events=("start", "end")):
        tag = _local(el.tag)
        if event == "start":
            tags.append(tag)
            if tag == "Language":
                lang = el.get("Identifier") or ""
            continue
        tags.pop()
        parent = tags[-1] if tags else ""
        if tag == "Hardware" and parent == "Hardware":
            hw_name, hw_id = el.get("Name"), el.get("Id")
            prods, apps = [], []
            for c in el.iter():
                k = _local(c.tag)
                if k == "Product":
                    p = {"id": c.get("Id"), "order_number": c.get("OrderNumber"),
                         "text": c.get("Text"), "default_language": c.get("DefaultLanguage"),
                         "hardware": _norm(hw_name), "hardware_id": hw_id}
                    prods.append(p["id"])
                    products[p["id"]] = p
                elif k == "ApplicationProgramRef":
                    apps.append(c.get("RefId"))
                elif k == "Hardware2Program":
                    h2p[c.get("Id")] = [x.get("RefId") for x in c.iter()
                                        if _local(x.tag) == "ApplicationProgramRef"]
            for a in apps:
                lst = programs.setdefault(a, [])
                for pid in prods:
                    if pid not in lst:
                        lst.append(pid)
            el.clear()
        elif tag == "TranslationElement":
            _collect_translation(langs, lang, el)
            el.clear()
        elif tag in ("TranslationUnit", "Language", "Languages"):
            el.clear()
    return {"programs": programs, "products": products, "langs": langs, "h2p": h2p}


def parse_catalog_xml(stream):
    """Catalog.xml -> (product, Hardware2Program) of every catalog item: the
    program a product is sold with.  A hardware can carry several programs
    (versions), and only the catalog says which product goes with which."""
    items = []
    for _event, el in ET.iterparse(stream, events=("end",)):
        if _local(el.tag) == "CatalogItem":
            if el.get("ProductRefId") and el.get("Hardware2ProgramRefId"):
                items.append((el.get("ProductRefId"), el.get("Hardware2ProgramRefId")))
            el.clear()
        elif _local(el.tag) in ("TranslationUnit", "Languages"):
            el.clear()
    return items


def parse_master_xml(stream):
    """knx_master.xml -> manufacturer names and mask versions (with procedures)."""
    mfrs, masks = {}, {}
    version = None
    tags = []
    for event, el in ET.iterparse(stream, events=("start", "end")):
        tag = _local(el.tag)
        if event == "start":
            tags.append(tag)
            if tag == "MasterData":
                version = el.get("Version")
            continue
        tags.pop()
        parent = tags[-1] if tags else ""
        if tag == "Manufacturer" and parent == "Manufacturers":
            mfrs[el.get("Id")] = {"name": el.get("Name"),
                                  "knx_id": _int(el.get("KnxManufacturerId"))}
            el.clear()
        elif tag == "MaskVersion" and parent == "MaskVersions":
            procs = []
            for p in el.iter():
                if _local(p.tag) == "Procedure":
                    procs.append({
                        "type": p.get("ProcedureType"), "subtype": p.get("ProcedureSubType"),
                        "access": p.get("Access"),
                        "steps": [[_local(s.tag), dict(s.attrib)] for s in p
                                  if _local(s.tag).startswith("LdCtrl")]})
            masks[el.get("Id")] = {"mask_version": _int(el.get("MaskVersion")),
                                   "name": el.get("Name"),
                                   "management_model": el.get("ManagementModel"),
                                   "medium": el.get("MediumTypeRefId"), "procedures": procs}
            el.clear()
        elif tag in ("Manufacturers", "MaskVersions", "DatapointTypes", "Languages"):
            el.clear()
    return {"version": version, "manufacturers": mfrs, "masks": masks}


# ---------------------------------------------------------------------------
# distillation: raw XML records -> one JSON record per application program
# ---------------------------------------------------------------------------

_ADDR_KEYS = {"Address", "InitPtr", "SavePtr", "SerialPtr", "Callback", "Seg0", "Seg1"}


def render_step(op, attrs):
    """('LdCtrlAbsSegment', {...}) -> 'AbsSegment LsmIdx=1 Address=0x4000 ...'."""
    parts = [op[6:] if op.startswith("LdCtrl") else op]
    for k, v in attrs.items():
        if k in _ADDR_KEYS and v is not None and v.isdigit():
            v = "0x%04X" % int(v)
        elif k == "InlineData" and v and len(v) > 32:
            v = "%s...(%d bytes)" % (v[:32], len(v) // 2)
        parts.append("%s=%s" % (k, v))
    return " ".join(parts)


def _pick_load_procedure(mask):
    procs = [p for p in (mask or {}).get("procedures", [])
             if p.get("type") == "Load" and p.get("subtype") in ("all", None)]
    return procs[0] if procs else None


def _is_intlike(s):
    return bool(re.match(r"^-?\d+$", s or ""))


_PH_HEAD = re.compile(r"^[\s:,;|/–-]+")
_PH_TAIL = re.compile(r"[\s:,;|/–-]+$")
_PH_GAP = re.compile(r"\s+([:,;])")


def _fill(text, lookup, depth):
    out, unresolved, emptied = [], 0, False
    i, n = 0, len(text)
    while i < n:
        j = text.find("{{", i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        k, level = j + 2, 1
        while k < n and level:
            if text.startswith("{{", k):
                level += 1
                k += 2
            elif text.startswith("}}", k):
                level -= 1
                k += 2
            else:
                k += 1
        if level:                           # unbalanced: leave the rest alone
            out.append(text[j:])
            break
        name, sep, default = text[j + 2:k - 2].partition(":")
        value = lookup(name.strip())
        if value:
            out.append(value)
        elif sep and depth < 4:             # ETS shows the default for an empty value
            d, u, e = _fill(default, lookup, depth + 1)
            out.append(d)
            unresolved += u
            emptied = emptied or e or not d
        elif value is not None:
            emptied = True
        else:
            out.append(text[j:k])
            unresolved += 1
        i = k
    return "".join(out), unresolved, emptied


def fill_placeholders(text, lookup):
    """Resolve ETS text placeholders {{name}} and {{name:default}}; a default
    may contain placeholders itself.  lookup(name) returns the value (empty
    allowed) or None when the name is unknown: such a placeholder stays in
    the text unless it has a default.  Separators left dangling by an empty
    value are removed ('Channel B: ' -> 'Channel B').  Returns (text,
    number of placeholders left)."""
    if not text or "{{" not in text:
        return text, 0
    out, unresolved, emptied = _fill(text, lookup, 0)
    if emptied:
        out = _PH_GAP.sub(r"\1", _PH_TAIL.sub("", _PH_HEAD.sub("", out)))
    return _norm(out), unresolved


def build_record(raw, langs, master, hw, source, rows=None):
    a = raw.attrs
    full_id = raw.full_id
    prefix = full_id + "_"

    def short(x):
        return x[len(prefix):] if x and x.startswith(prefix) else x

    def short_ref(ref_id, base_id):
        return ref_id[len(base_id) + 1:] if ref_id and base_id and ref_id.startswith(base_id + "_") \
            else short(ref_id)

    m = _KNX_APP_RE.match(full_id)
    mfr_hex = m.group(1).upper() if m else "0000"
    number = _int(a.get("ApplicationNumber"))
    version = _int(a.get("ApplicationVersion"))
    if number is None and m:
        number = int(m.group(2), 16)
    if version is None and m:
        version = int(m.group(3), 16)
    if number is None or version is None:
        raise IngestError("%s: no ApplicationNumber/ApplicationVersion" % full_id)
    app_id = "%04X%04X%02X" % (int(mfr_hex, 16), number, version)

    mfrs = master.get("manufacturers", {})

    def manufacturer(ref):
        knx_id = int(ref[2:], 16) if ref and re.match(r"^M-[0-9A-Fa-f]{4}$", ref) else None
        info = mfrs.get(ref) or {}
        return {"id": ref, "knx_id": knx_id, "name": info.get("name")}

    mfr = manufacturer("M-" + mfr_hex)
    orig = None
    aliases = []
    if a.get("OriginalManufacturer"):
        orig = manufacturer(a.get("OriginalManufacturer"))
        if orig["knx_id"] is not None:
            alias = "%04X%04X%02X" % (orig["knx_id"], number, version)
            if alias != app_id:
                aliases.append(alias)

    en, de = _merge_langs(langs)
    T = _Texts(en, de, a.get("DefaultLanguage"))
    name, name_de = T.get(full_id, "Name", a.get("Name"))

    # -- types, enum texts in English
    types_out = {}
    for tid, t in raw.types.items():
        o = {k: v for k, v in t.items() if k != "values" and v is not None}
        if t.get("kind") == "enum":
            o["values"] = [[v["value"], T.get(v["id"], "Text", v["text"])[0]]
                           for v in t.get("values", [])]
            binary = [[v["binary"], v["value"]] for v in t.get("values", []) if v.get("binary")]
            if binary:
                o["binary"] = binary            # stored bytes (hex) -> value
        types_out[short(tid)] = o

    # -- load procedure according to LoadProcedureStyle
    style = a.get("LoadProcedureStyle") or ""
    mask_id = a.get("MaskVersion")
    mask = master.get("masks", {}).get(mask_id)
    plain = [s for mid, steps in raw.ldprocs if not mid for s in steps]
    merge = {}
    for mid, steps in raw.ldprocs:
        if mid:
            merge.setdefault(str(mid), []).extend(steps)
    proc = _pick_load_procedure(mask)
    if style == "ProductProcedure" or not proc:
        steps = plain + [s for mid in sorted(merge) for s in merge[mid]]
        lp_source = "application" if style == "ProductProcedure" else \
            "application (no Load procedure for %s in knx_master)" % mask_id
    else:
        steps = []
        for op, at in proc["steps"]:
            if op == "LdCtrlMerge":
                steps.extend(merge.get(str(at.get("MergeId")), []))
            else:
                steps.append((op, at))
        lp_source = "knx_master %s Load/%s" % (mask_id, proc.get("subtype"))
        if merge:
            lp_source += " merged with application"

    # -- segments, annotated with their LdCtrlAbsSegment
    segs_out = {}
    for sid, s in raw.segments.items():
        o = {"id": short(sid)}
        if s["kind"] == "absolute":
            o["address"] = s["address"]
            if s["address"] is not None:
                o["address_hex"] = "0x%04X" % s["address"]
        else:
            o["relative"] = True
            o["offset"] = s.get("offset")
            if s.get("lsm") is not None:
                o["lsm"] = s["lsm"]
        o["size"] = s.get("size")
        segs_out[sid] = o
    for op, at in steps:
        if op == "LdCtrlAbsSegment":
            addr = _int(at.get("Address"))
            for sid, s in raw.segments.items():
                if s["kind"] == "absolute" and s["address"] == addr:
                    for key, attr in (("lsm", "LsmIdx"), ("mem_type", "MemType"),
                                      ("access", "Access"), ("seg_flags", "SegFlags")):
                        v = _int(at.get(attr))
                        if v is not None:
                            segs_out[sid][key] = v

    # -- table locations
    tables = {}
    for xml_name, key in (("AddressTable", "address_table"),
                          ("AssociationTable", "association_table"),
                          ("ComObjectTable", "comobject_table")):
        t = raw.tables.get(xml_name)
        if t is None:
            continue
        o = {}
        seg_id, off = t.get("CodeSegment"), _int(t.get("Offset"))
        if seg_id:
            o["segment"] = short(seg_id)
        if off is not None:
            o["offset"] = off
        seg = raw.segments.get(seg_id)
        if seg and seg["kind"] == "absolute" and seg["address"] is not None and off is not None:
            o["address"] = seg["address"] + off
            o["address_hex"] = "0x%04X" % o["address"]
        if t.get("MaxEntries") is not None:
            o["max_entries"] = _int(t.get("MaxEntries"))
        tables[key] = o

    # -- lookups over the program and its module definitions
    defs = raw.module_defs
    all_params = list(raw.params)
    all_prefs = list(raw.param_refs)
    all_corefs = list(raw.comobject_refs)
    for md in defs.values():
        all_params += md.params
        all_prefs += md.param_refs
        all_corefs += md.comobject_refs
    params_by_id = {p["id"]: p for p in all_params}
    pref_by_id = {r["id"]: r for r in all_prefs}
    refs_by_param = collections.defaultdict(list)
    for r in all_prefs:
        refs_by_param[r["ref"]].append(r)
    corefs = collections.defaultdict(list)
    for r in all_corefs:
        corefs[r["ref"]].append(r)
    refs_by_number = collections.defaultdict(list)       # "226" -> [..._P-1854_R-226]
    for r in all_prefs:
        mm = re.search(r"_R-(\d+)$", r["id"] or "")
        if mm:
            refs_by_number[mm.group(1)].append(r["id"])

    # -- texts with ETS placeholders: {{argument}} = module instance argument,
    # {{0}} = value of the element's text parameter (TextParameterRefId),
    # {{226}} (older schemas) = value of the parameter behind ParameterRef R-226
    zero_cache = {}

    def zero_value(ref_id):
        """(en, de) default of a text parameter, None if unknown."""
        if not ref_id:
            return None
        if ref_id not in zero_cache:
            r = pref_by_id.get(ref_id)
            p = params_by_id.get(r["ref"] if r else ref_id)
            v = None
            if r is not None and r.get("default") is not None:
                v = T.get(r["id"], "Value", r["default"])
            elif p is not None and p.get("default") is not None:
                v = T.get(p["id"], "Value", p["default"])
            zero_cache[ref_id] = v
        return zero_cache[ref_id]

    def text2(elem_id, attr, raw_text, names=None, zero=None):
        t_en, t_de = T.get(elem_id, attr, raw_text)
        if "{{" not in (t_en or "") and "{{" not in (t_de or ""):
            return t_en, t_de
        z = zero_value(zero)

        def lookup(idx):
            def f(key):
                if key.isdigit():
                    v = z if key == "0" else None
                    if v is None and len(refs_by_number.get(key, ())) == 1:
                        v = zero_value(refs_by_number[key][0])
                else:
                    v = (names or {}).get(key)
                if v is None:
                    return None
                return v[idx] if v[idx] is not None else v[0]
            return f

        if t_en:
            t_en = fill_placeholders(t_en, lookup(0))[0]
        if t_de:
            t_de = fill_placeholders(t_de, lookup(1))[0]
            if t_de == t_en:
                t_de = None
        return t_en, t_de

    # -- ETS conditions (Dynamic) rendered as text
    def pref_text(pref_id, names=None):
        r = pref_by_id.get(pref_id)
        if r is None:
            return short(pref_id) or "?", None
        p = params_by_id.get(r["ref"])
        text = None
        if r.get("text"):
            text = text2(pref_id, "Text", r["text"], names, r.get("text_param"))[0]
        elif p is not None:
            text = text2(p["id"], "Text", p["text"], names, p.get("text_param"))[0]
        if not text and p is not None:
            text = _norm(p.get("name")) or short(p["id"])
        return text or short(pref_id), p

    def value_text(p, value):
        if p is None or not p.get("type"):
            return None
        ty = types_out.get(short(p["type"]))
        if not ty or ty.get("kind") != "enum":
            return None
        for v, txt in ty["values"]:
            if v == value:
                return txt
        return None

    def render_cond(cond, names=None):
        pref, test, others = cond
        text, p = pref_text(pref, names)
        if test is not None:
            vals = test.split()
            if vals and all(_is_intlike(v) for v in vals):
                # Every value counts — a list cut at six made a device with
                # the seventh look out of scope.  Texts for the first six only.
                shown = []
                for i, v in enumerate(vals):
                    vt = value_text(p, v) if i < 6 else None
                    shown.append("%s (%s)" % (v, vt) if vt else v)
                if len(vals) == 1:
                    return "%s = %s" % (text, shown[0])
                return "%s in {%s}" % (text, ", ".join(shown))
            return "%s %s" % (text, test)
        return "%s = other (not %s)" % (text, " | ".join(others))

    def block_label(b, names=None):
        if b is None:
            return None
        _kind, bid, text, pref_id, nm, text_param = b
        if text:
            return text2(bid, "Text", text, names, text_param)[0] or None
        if pref_id:
            return pref_text(pref_id, names)[0] or None
        return _norm(nm) or None

    def context(entry, inst=None):
        """when / when_any / block / seen of a UI reference; for a module
        instance its own conditions come first, then the innermost ones of
        the module.  Every place the UI shows it is one set of conditions
        (_when_fields)."""
        names = inst["names"] if inst else None
        outer = inst["outer"] if inst else []
        alts, cut = [], False
        for conds in (entry[1] if entry else [()]):
            alt = list(outer) + [render_cond(c, names) for c in conds]
            if len(alt) > MAX_CONDS:            # outermost kept: they name the mode
                alt, cut = alt[:MAX_CONDS], True
            alts.append(alt)
        o = _when_fields(alts)
        if cut:
            o["when_more"] = True
        if entry is None:
            o["ui"] = False
            return o
        bl = block_label(entry[2], names)
        if bl:
            o["block"] = bl
        if entry[0] > 1:
            o["seen"] = entry[0]
        return o

    unsupported = collections.Counter(raw.unsupported)

    # -- module instances: every <Module> (each repetition of a <Repeat>) gets
    # its argument values; BaseOffset/BaseNumber arguments place its records
    def repeat_max(pref_id):
        r = pref_by_id.get(pref_id)
        p = params_by_id.get(r["ref"]) if r else None
        ty = raw.types.get(p["type"]) if p and p.get("type") else None
        if ty and ty.get("kind") == "number" and isinstance(ty.get("max"), int):
            return ty["max"]
        if ty and ty.get("kind") == "enum":
            vals = [_intval(v["value"]) for v in ty.get("values", [])]
            vals = [v for v in vals if v is not None]
            return max(vals) if vals else None
        return None

    instances = []
    inst_per_def = collections.Counter()
    alloc_next = {lid: al.get("start") for lid, al in raw.allocators.items()}
    alloc_users = collections.Counter(
        g["allocator"] for mod in raw.dynamic.get("modules", [])
        for g in mod["args"].values() if g.get("allocator"))
    for mod in raw.dynamic.get("modules", []):
        md = defs.get(mod["ref"])
        if md is None:
            unsupported["module_def_missing"] += 1
            continue
        outer = [render_cond(c) for c in mod["conds"]]     # every condition placing it
        rep = mod.get("repeat")
        count, count_pref = 1, None
        if rep is not None:
            if rep.get("count") is not None:
                count = rep["count"]
            elif rep.get("pref"):
                count, count_pref = repeat_max(rep["pref"]), rep["pref"]
            else:
                count = None
            if count is None or count < 0:
                unsupported["repeat_count_unknown"] += 1
                continue
            if count > MAX_REPEAT:
                unsupported["repeat_instances_capped"] += count - MAX_REPEAT
                count = MAX_REPEAT
        group = []
        for i in range(1, count + 1):
            values, names, ok = {}, {}, True
            for arg_id, decl in md.args.items():
                given = mod["args"].get(arg_id)
                if given is None:
                    continue
                if given.get("allocator"):
                    lid = given["allocator"]
                    al = raw.allocators.get(lid)
                    step = decl.get("allocates")
                    if al is None or alloc_next.get(lid) is None or not step:
                        unsupported["allocator_unresolved"] += 1
                        continue
                    v = alloc_next[lid]
                    alloc_next[lid] = v + step
                    if al.get("max") is not None and v + step > al["max"]:
                        ok = False
                    if alloc_users[lid] > 1:
                        unsupported["allocator_shared"] += 1
                    values[arg_id] = v
                    names[decl["name"]] = (str(v), None)
                elif given.get("text_id") is not None or decl.get("text"):
                    t_en, t_de = T.get(given.get("text_id"), "Value", given.get("value"))
                    values[arg_id] = t_en
                    names[decl["name"]] = (t_en, t_de)
                else:
                    values[arg_id] = _int(given.get("value"))
                    names[decl["name"]] = (_norm(given.get("value")) or "", None)
            if not ok:
                unsupported["allocator_overflow"] += 1
                continue
            iid = short(mod["id"]) + ("_MI-%d" % i if rep is not None else "")
            o_outer = list(outer)
            if count_pref:
                o_outer.append("%s >= %d" % (pref_text(count_pref)[0], i))
            label = block_label(md.dynamic.get("first_block"), names)
            label = label or _norm(mod.get("name")) or "%s %d" % (
                md.attrs.get("Name") or short(md.full_id), i)
            group.append({"id": iid, "md": md, "values": values, "names": names,
                          "outer": o_outer, "label": label, "index": i})
        if rep is not None and len({g["label"] for g in group}) < len(group):
            for g in group:
                g["label"] = "%s #%d" % (g["label"], g["index"])
        instances.extend(group)
        inst_per_def[mod["ref"]] += len(group)

    def local_id(x, md):
        p = md.full_id + "_"
        return x[len(p):] if x.startswith(p) else short(x)

    # -- parameters
    blank_tpl = {sid: not d or max(collections.Counter(d).values()) >= 0.95 * len(d)
                 for sid, d in raw.seg_data.items()}
    counts = collections.Counter()
    tcheck = collections.Counter()
    declared_order = raw.options.get("ParameterByteOrder")
    order = byte_order_of(declared_order)
    # discriminating evidence from the segment template (see below)
    evidence = collections.Counter()

    def make_param(p, inst):
        pid = p["id"]
        names = inst["names"] if inst else None
        loc = p.get("loc")
        if loc and loc.get("base"):
            bv = inst["values"].get(loc["base"]) if inst else None
            if loc["kind"] != "memory" or not isinstance(bv, int):
                unsupported["parameters_unplaced"] += 1
                return None
            loc = dict(loc, offset=loc["offset"] + bv)
        text, text_de = text2(pid, "Text", p["text"], names, p.get("text_param"))
        o = {"id": inst["id"] + "_" + local_id(pid, inst["md"]) if inst else short(pid),
             "text": text}
        if text_de:
            o["text_de"] = text_de
        if inst:
            o["module"] = {"id": inst["id"], "label": inst["label"]}
        nm = _norm(p.get("name"))
        if nm and nm != text and nm != text_de:
            o["name"] = nm
        access = p.get("access") or "ReadWrite"
        tshort = short(p.get("type")) if p.get("type") else None
        ty = types_out.get(tshort) if tshort else None
        if tshort:
            o["type"] = tshort
        size = ty.get("size_bits") if ty else None
        if size is not None:
            o["size_bits"] = size
        if loc and loc["kind"] == "memory":
            o["storage"] = "memory"
            seg = raw.segments.get(loc["segment"])
            o["segment"] = short(loc["segment"])
            o["offset"] = loc["offset"]
            o["bit_offset"] = loc["bit_offset"]
            if seg and seg["kind"] == "absolute" and seg["address"] is not None:
                o["address"] = seg["address"] + loc["offset"]
            elif seg and seg["kind"] == "relative":
                o["relative"] = True
            counts["parameters_memory"] += 1
        elif loc:
            o["storage"] = "property"
            o["property"] = {k: v for k, v in loc.items() if k != "kind"}
            counts["parameters_property"] += 1
        else:
            counts["parameters_virtual"] += 1
        if "union" in p:
            o["union"] = p["union"]
            counts["parameters_union"] += 1
        # What ETS shows comes from the refs: each can bring its own text,
        # default and access, and a ref with Access None sits in the tree but
        # is never shown.  The parameter takes what its VISIBLE refs agree on;
        # a ref that differs is listed with the conditions that show it.  The
        # <Parameter>'s own attributes served "access None" for parameters
        # every visible ref makes writable, and one default where each channel
        # mode has its own (review 2026-10-29).
        dyn_pr = (inst["md"].dynamic if inst else raw.dynamic)["pr"]
        refs = []
        for r in refs_by_param.get(pid, []):
            counts["parameter_refs"] += 1
            e = dyn_pr.get(r["id"])
            acc = r.get("access") or access
            rt, rt_de = (text2(r["id"], "Text", r["text"], names, r.get("text_param"))
                         if r.get("text") else (None, None))
            refs.append({"id": r["id"], "entry": e, "access": acc, "text": rt, "text_de": rt_de,
                         "default": r["default"] if r.get("default") is not None
                         else p.get("default"),
                         "visible": e is not None and acc != "None"})
        vis = [x for x in refs if x["visible"]]
        if vis and all(x["text"] for x in vis):     # ETS never shows the parameter's own text
            o["text"] = collections.Counter(x["text"] for x in vis).most_common(1)[0][0]
            de = next((x["text_de"] for x in vis if x["text"] == o["text"] and x["text_de"]), None)
            if de:
                o["text_de"] = de
            else:
                o.pop("text_de", None)
        top_access, top_default = access, p.get("default")
        if vis:
            top_access = collections.Counter(x["access"] for x in vis).most_common(1)[0][0]
            defaults = {x["default"] for x in vis}
            top_default = defaults.pop() if len(defaults) == 1 else None
        if top_access != "ReadWrite":
            o["access"] = top_access
        if top_default is not None:
            o["default"] = top_default
        variants = []
        for x in refs:
            v = {}
            if x["text"] and x["text"] != o["text"]:
                v["text"] = x["text"]
            if x["default"] is not None and x["default"] != top_default:
                v["default"] = x["default"]
            if x["access"] != top_access:
                v["access"] = x["access"]
            if not v:
                continue
            v["ref"] = short_ref(x["id"], pid)
            ctx = context(x["entry"], inst)
            for k in ("when", "when_any", "when_any_more", "when_more"):
                if k in ctx:
                    v[k] = ctx[k]
            if not x["visible"]:
                v["ui"] = False
            variants.append(v)
        if variants:
            o["variants"] = variants[:8]
            if len(variants) > 8:
                o["variants_total"] = len(variants)
        shown = [x["entry"] for x in vis]
        hidden = [x["entry"] for x in refs if x["entry"] is not None and not x["visible"]]
        # Every visible reference counts: a parameter the UI shows through one
        # ref in blind mode and through another in roller-shutter mode applies
        # in both, and taking the first ref's conditions alone said otherwise.
        # Hidden refs say nothing about when ETS shows it — counting them made
        # a status parameter look visible with status signals switched off.
        # Shown nowhere: ui false, and `when` tells where it sits in the tree.
        if shown:
            o.update(context(_merge_entries(shown), inst))
        else:
            ctx = context(_merge_entries(hidden), inst)
            ctx.pop("seen", None)
            ctx["ui"] = False
            o.update(ctx)
        # Self-check of the layout conventions against the manufacturer's
        # segment template, which usually (not always) carries the defaults.
        # Only cases where the alternatives give DIFFERENT values count as
        # evidence: MSB- vs LSB-first bit offsets, big- vs little-endian.
        if (loc and loc["kind"] == "memory" and ty and ty.get("kind") in ("number", "enum")
                and size and size <= 64 and (p.get("union") is None or p["union"]["default"])):
            dv = _intval(p.get("default"))
            data = raw.seg_data.get(loc["segment"])
            if data is not None and blank_tpl.get(loc["segment"]):
                # A template of (nearly) one byte value — zeros, or 0xFF of erased
                # flash — carries no defaults: checking against it reported a
                # layout problem that does not exist.
                tcheck["skipped_empty_template"] += 1
                data = None
            if dv is not None and data is not None:
                dv &= (1 << size) - 1
                off, bit = loc["offset"], loc["bit_offset"]
                tv = extract_bits(data, off, bit, size, order)
                if tv is not None:
                    tcheck["checked"] += 1
                    tcheck["match"] += int(tv == dv)
                    if inst:
                        tcheck["module_checked"] += 1
                        tcheck["module_match"] += int(tv == dv)
                if bit + size <= 8 and size < 8 and off < len(data):
                    msb = (data[off] >> (8 - bit - size)) & ((1 << size) - 1)
                    lsb = (data[off] >> bit) & ((1 << size) - 1)
                    if msb != lsb and dv in (msb, lsb):
                        evidence["msb_first" if dv == msb else "lsb_first"] += 1
                if bit == 0 and size > 8 and size % 8 == 0 and off + size // 8 <= len(data):
                    chunk = data[off:off + size // 8]
                    be, le = int.from_bytes(chunk, "big"), int.from_bytes(chunk, "little")
                    if be != le and dv in (be, le):
                        evidence["big" if dv == be else "little"] += 1
        return o

    # -- com objects with all ComObjectRef variants, fully resolved
    def make_comobject(c, inst):
        cid = c["id"]
        names = inst["names"] if inst else None
        num = c["number"]
        if c.get("base_number"):
            bv = inst["values"].get(c["base_number"]) if inst else None
            if not isinstance(bv, int) or num is None:
                unsupported["comobjects_unplaced"] += 1
                return None
            num += bv
        text, text_de = text2(cid, "Text", c["text"], names, c.get("text_param"))
        ft, ft_de = text2(cid, "FunctionText", c["function_text"], names, c.get("text_param"))
        o = {"number": num, "id": inst["id"] + "_" + local_id(cid, inst["md"]) if inst else short(cid),
             "text": text}
        if text_de:
            o["text_de"] = text_de
        if inst:
            o["module"] = {"id": inst["id"], "label": inst["label"]}
        if ft:
            o["function_text"] = ft
        if ft_de:
            o["function_text_de"] = ft_de
        nm = _norm(c.get("name"))
        if nm and nm not in (text, text_de):
            o["name"] = nm
        o["size"] = c.get("size")
        o["size_bits"] = size_bits(c.get("size"))
        o["flags"] = flags_str(c["flags"])
        o["priority"] = c.get("priority")
        if c.get("dpt"):
            o["dpt"] = fmt_dpt(c["dpt"])
        variants = []
        dyn_co = (inst["md"].dynamic if inst else raw.dynamic)["co"]
        for r in corefs.get(cid, []):
            v = {"ref": short_ref(r["id"], cid)}
            tp = r.get("text_param") or c.get("text_param")
            if r.get("text"):
                v["text"], vt_de = text2(r["id"], "Text", r["text"], names, tp)
            else:
                v["text"], vt_de = text2(cid, "Text", c["text"], names, tp)
            if vt_de:
                v["text_de"] = vt_de
            if r.get("function_text"):
                v["function_text"] = text2(r["id"], "FunctionText", r["function_text"],
                                           names, tp)[0]
            elif ft:
                v["function_text"] = ft
            vsize = r.get("size") or c.get("size")
            v["size"] = vsize
            v["size_bits"] = size_bits(vsize)
            fl = {k: (r["flags"].get(k) if r["flags"].get(k) is not None else c["flags"].get(k))
                  for k, _ in _FLAG_ATTRS}
            v["flags"] = flags_str(fl)
            v["priority"] = r.get("priority") or c.get("priority")
            dpt = r.get("dpt") or c.get("dpt")
            if dpt:
                v["dpt"] = fmt_dpt(dpt)
            v.update(context(dyn_co.get(r["id"]), inst))
            variants.append(v)
        refs = corefs.get(cid, [])
        if text and ("{{" in text or (refs and all(r.get("text") for r in refs))):
            # Show what the variants resolve to when the object's own text only
            # names a placeholder ({{0}} without a text parameter), or when
            # every ref brings its own text, so ETS never shows the object's:
            # a module template says "Channel A" for the object of every channel.
            best = collections.Counter(v["text"] for v in variants
                                       if v.get("text") and "{{" not in v["text"]).most_common(1)
            if best:
                o["text"] = best[0][0]
                de = next((v["text_de"] for v in variants
                           if v.get("text") == o["text"] and v.get("text_de")), None)
                if de:
                    o["text_de"] = de
                else:
                    o.pop("text_de", None)
        o["variants"] = variants
        counts["comobject_refs"] += len(variants)
        return o

    # Parameters and com objects go to `rows` as soon as they are made: the
    # index keeps them as rows, and a program with half a million parameters
    # does not fit into memory as dicts.  Without `rows` the record holds them.
    sink = rows if rows is not None else _RowList()
    made = collections.Counter()

    def add(kind, o):
        if o is None:
            return 0
        made[kind] += 1
        made["placeholders"] += "{{" in (o.get("text") or "")
        if kind == "comobject":
            made["placeholders"] += sum(1 for v in o["variants"] if "{{" in (v.get("text") or ""))
        sink.add(kind, o)
        return 1

    for p in raw.params:
        add("parameter", make_param(p, None))
    for c in raw.comobjects:
        add("comobject", make_comobject(c, None))
    mod_params = mod_cos = 0
    for inst in instances:
        for p in inst["md"].params:
            mod_params += add("parameter", make_param(p, inst))
        for c in inst["md"].comobjects:
            mod_cos += add("comobject", make_comobject(c, inst))

    # -- products
    prods = []
    hw_texts = _Texts(*_merge_langs(hw.get("langs", {})), default_lang=None)
    for pid in hw.get("programs", {}).get(full_id, []):
        p = hw["products"].get(pid)
        if not p:
            continue
        t, t_de = hw_texts.get(pid, "Text", p.get("text"), p.get("default_language"))
        po = {"order_number": p.get("order_number"), "text": t}
        if t_de:
            po["text_de"] = t_de
        if p.get("hardware"):
            po["hardware"] = p["hardware"]
        prods.append(po)
    prods.sort(key=lambda p: p.get("order_number") or "")

    mask_out = {"id": mask_id}
    if mask:
        if mask.get("mask_version") is not None:
            mask_out["version_hex"] = "0x%04X" % mask["mask_version"]
        for k in ("name", "management_model"):
            if mask.get(k):
                mask_out[k] = mask[k]

    left = made["placeholders"]
    counts = {"comobjects": made["comobject"], "comobject_refs": counts["comobject_refs"],
              "parameters": made["parameter"], "parameters_memory": counts["parameters_memory"],
              "parameters_union": counts["parameters_union"],
              "parameters_property": counts["parameters_property"],
              "parameters_virtual": counts["parameters_virtual"],
              "parameter_refs": counts["parameter_refs"],
              "template_check": {"checked": tcheck["checked"], "match": tcheck["match"]}}
    if tcheck["skipped_empty_template"]:
        counts["template_check"]["skipped_empty_template"] = tcheck["skipped_empty_template"]
    if defs or instances:
        counts.update({"module_defs": len(defs), "module_instances": len(instances),
                       "module_parameters": mod_params, "module_comobjects": mod_cos,
                       "template_check_modules": {"checked": tcheck["module_checked"],
                                                  "match": tcheck["module_match"]}})
    if unsupported:
        counts["module_unsupported"] = dict(sorted(unsupported.items()))
    if left:
        counts["texts_with_placeholders"] = left
    byte_order = {"declared": declared_order or "not stated (BigEndian assumed)",
                  "used": order,
                  "template_evidence": {"big": evidence["big"], "little": evidence["little"]},
                  "bit_order_evidence": {"msb_first": evidence["msb_first"],
                                         "lsb_first": evidence["lsb_first"]}}
    # Warn on evidence, not on one coincidence: a single default that happens
    # to match the reversed bytes warned programs whose template agrees with
    # their declared order 165 times out of 167.
    other = "little" if order == "big" else "big"
    contra, pro = evidence[other], evidence[order]
    if contra >= BYTE_ORDER_MIN_EVIDENCE and contra > 2 * pro:
        byte_order["warning"] = (
            "multi-byte defaults in this program's segment template read as %s-endian, "
            "although it declares %s; check multi-byte values on a device before "
            "writing them" % (other, declared_order or "nothing"))
    elif contra >= BYTE_ORDER_MIN_EVIDENCE and pro >= BYTE_ORDER_MIN_EVIDENCE and contra > pro:
        byte_order["warning"] = (
            "this program's segment template mixes byte orders (%d multi-byte defaults read "
            "big-endian, %d little-endian), it declares %s; check multi-byte values on a "
            "device before writing them" % (evidence["big"], evidence["little"],
                                            declared_order or "nothing"))

    rec = {
        "schema": SCHEMA_VERSION, "app_id": app_id, "aliases": aliases, "id": full_id,
        "name": name, "manufacturer": mfr, "application_number": number,
        "application_number_hex": "%04X" % number, "application_version": version,
        "application_version_hex": "%02X" % version, "program_type": a.get("ProgramType"),
        "mask": mask_out, "load_procedure_style": style,
        "default_language": a.get("DefaultLanguage"),
        "converted_from_pre_ets4": (a.get("ConvertedFromPreEts4Data") or "").lower() in ("1", "true"),
        "products": prods,
        "segments": sorted(segs_out.values(), key=lambda s: (s.get("address") is None,
                                                             s.get("address") or 0, s["id"])),
        "tables": tables,
        "load_procedure": {"source": lp_source, "steps": [render_step(op, at) for op, at in steps]},
        "byte_order": byte_order,
        "counts": counts, "types": types_out,
        "source": source,
    }
    if defs:
        # arguments: name -> step an allocator advances by (null if none), "text"
        rec["modules"] = [{"id": short(did), "name": md.attrs.get("Name"),
                           "arguments": {d["name"]: "text" if d["text"] else d["allocates"]
                                         for d in md.args.values()},
                           "parameters": len(md.params), "comobjects": len(md.comobjects),
                           "instances": inst_per_def[did]}
                          for did, md in defs.items()]
    if name_de:
        rec["name_de"] = name_de
    if orig:
        rec["original_manufacturer"] = orig
    if rows is None:
        rec["parameters"], rec["comobjects"] = sink.ordered()
    return rec


def _param_order(o, seq):
    """Index order of parameters: by address, then by segment and offset, then
    property and ETS-only parameters; `seq` keeps the program's own order."""
    if o.get("address") is not None:
        return (0, "", o["address"], o.get("bit_offset") or 0, seq)
    if o.get("storage") == "memory":
        return (1, o.get("segment") or "", o.get("offset") or 0, o.get("bit_offset") or 0, seq)
    return (2 if o.get("storage") else 3, "", 0, 0, seq)


def _comobject_order(o, seq):
    return (o["number"] if o["number"] is not None else 1 << 30, o["id"], seq)


def _pack(obj):
    return zlib.compress(_dumps(obj).encode("utf-8"), 6)


def _unpack(blob):
    return json.loads(zlib.decompress(blob))


def _haystack(fields):
    """What a query searches: the fields as one string in lower case, each
    text once (variants repeat their object's texts; a query word cannot
    span two fields, so that changes no match)."""
    return " ".join(dict.fromkeys(f.lower() for f in fields if f))


def _module_cols(o):
    m = o.get("module") or {}
    return (m.get("id") or "").lower() or None, (m.get("label") or "").lower() or None


def _param_row(o):
    """A parameter as an index row: storage, segment, address, offset in the
    segment, size_bits, bytes occupied, module id and label (lower case),
    search text, and the body last."""
    mem = o.get("storage") == "memory"
    fields = [o.get("text"), o.get("text_de"), o.get("name"), o.get("block"), o.get("id"),
              (o.get("module") or {}).get("label")]
    fields += [v.get("text") for v in o.get("variants", [])]
    return ((o.get("storage"), o.get("segment") if mem else None, o.get("address"),
             o.get("offset") if mem else None, o.get("size_bits"),
             _param_nbytes(o) if mem else None) + _module_cols(o)
            + (_haystack(fields), _pack(o)))


def _comobject_row(o):
    """A com object as an index row: number, module id and label, search
    text, body."""
    fields = [o.get("text"), o.get("text_de"), o.get("function_text"),
              o.get("function_text_de"), o.get("name"), (o.get("module") or {}).get("label")]
    for v in o.get("variants", []):
        fields += [v.get("text"), v.get("text_de"), v.get("function_text")]
    return (o.get("number"),) + _module_cols(o) + (_haystack(fields), _pack(o))


class _RowList:
    """Where build_record puts parameters and com objects by default: kept as
    dicts, handed out in index order."""

    def __init__(self):
        self.parameters, self.comobjects = [], []

    def add(self, kind, o):
        (self.parameters if kind == "parameter" else self.comobjects).append(o)

    def ordered(self):
        p = sorted(enumerate(self.parameters), key=lambda x: _param_order(x[1], x[0]))
        c = sorted(enumerate(self.comobjects), key=lambda x: _comobject_order(x[1], x[0]))
        return [o for _, o in p], [o for _, o in c]


def _layout_term(t):
    return int.from_bytes(hashlib.sha256(repr(t).encode("utf-8")).digest()[:16], "big")


class _RowSink:
    """Parameters and com objects of one program as index rows, made while
    build_record produces them.  As dicts, a program with half a million
    parameters took 2.3 GB; as rows with compressed bodies a fraction.

    It also sums up the program's layout: where each parameter is stored and
    how big it is, each com object's number with the sizes and flags of its
    variants.  Two programs with the same ApplicationId and the same layout
    read and decode a device alike (one product file may carry a program
    converted from ETS3, another the same program in ETS4 form)."""

    def __init__(self):
        self.parameters, self.comobjects = [], []
        self.max_nbytes = 1
        self.layout = 0                     # a sum: the order of the rows does not count

    def add(self, kind, o):
        if kind == "parameter":
            row = _param_row(o)
            if row[5]:
                self.max_nbytes = max(self.max_nbytes, row[5])
            self.parameters.append((_param_order(o, len(self.parameters)), row))
            if o.get("storage"):
                pr = o.get("property") or {}
                self.layout += _layout_term((
                    "p", o["storage"], o.get("segment"), o.get("address"), o.get("offset"),
                    o.get("bit_offset"), o.get("size_bits"), pr.get("object_index"),
                    pr.get("object_type"), pr.get("pid"), pr.get("offset"), pr.get("bit_offset"),
                    pr.get("occurrence") or 0))
        else:
            self.comobjects.append((_comobject_order(o, len(self.comobjects)),
                                    _comobject_row(o)))
            self.layout += _layout_term((
                "c", o.get("number"), o.get("size_bits"),
                tuple(sorted({(v.get("size_bits") or 0, v.get("flags") or "")
                              for v in o.get("variants", [])}))))

    def fingerprint(self, byte_order):
        return hashlib.sha256(("%x|%s" % (self.layout % (1 << 128), byte_order)).encode(
            "ascii")).hexdigest()[:32]

    def ordered(self):
        self.parameters.sort(key=lambda x: x[0])
        self.comobjects.sort(key=lambda x: x[0])
        return [r for _, r in self.parameters], [r for _, r in self.comobjects]


# ---------------------------------------------------------------------------
# input enumeration: directory, zip, nested zip (.knxprod in .zip)
# ---------------------------------------------------------------------------

class IngestError(Exception):
    pass


_APP_FILE_RE = re.compile(r"^m-[0-9a-f]{4}_a-[0-9a-f]{4}-[0-9a-f]{2}-.*\.xml$")


def classify_member(name):
    path = name.replace("\\", "/")
    base = path.rsplit("/", 1)[-1].lower()
    # macOS copies carry AppleDouble shadows (`._name`, under `__MACOSX/`) that
    # share the name of the real file but hold no product data; one
    # manufacturer bundle produced 80 "not a zip" warnings from them.
    if base.startswith("._") or "__MACOSX/" in path:
        return None
    if base == "knx_master.xml":
        return "master"
    if base == "hardware.xml":
        return "hardware"
    if base == "catalog.xml":
        return "catalog"
    if _APP_FILE_RE.match(base):
        return "app"
    if base.endswith(".knxprod") or base.endswith(".zip"):
        return "container"
    return None


def safe_member_name(name):
    """Reject absolute paths, drive letters, '..' components and NUL bytes."""
    if not name or "\x00" in name:
        return False
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return False
    return ".." not in n.split("/")


class _Member:
    def __init__(self, name, kind, opener, size):
        self.name, self.kind, self._opener, self.size = name, kind, opener, size

    def open(self):
        return self._opener()


class SourceSet:
    """Collect master/hardware/application XML members from a path.

    Zip safety: member names are checked (no traversal), declared uncompressed
    sizes are summed over all nesting levels and capped, the member count is
    capped, nesting depth is capped.  zipfile itself never returns more than
    a member's declared size and verifies the CRC."""

    def __init__(self, path, tmp_dir, max_total=None, max_members=ZIP_MAX_MEMBERS,
                 max_depth=ZIP_MAX_DEPTH, label=None):
        self.path = path
        self.label = label
        self.tmp_dir = tmp_dir
        self.max_total = ZIP_MAX_TOTAL if max_total is None else max_total   # set by --zip-max-mb
        self.max_members, self.max_depth = max_members, max_depth
        self.members = []
        self.warnings = []
        self._zips = []
        self._tmpfiles = []
        self._total = 0
        self._count = 0

    def __enter__(self):
        try:
            self._add(self.path, 0,
                      self.label or os.path.basename(self.path.rstrip("/")) or self.path)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        for z in self._zips:
            z.close()
        self._zips = []
        for t in self._tmpfiles:
            try:
                os.remove(t)
            except FileNotFoundError:
                pass
        self._tmpfiles = []

    def _add(self, path, depth, label):
        if os.path.isdir(path):
            self._add_dir(path, depth)
        elif zipfile.is_zipfile(path):
            self._add_zip(path, depth, label)
        elif os.path.isfile(path) and classify_member(path) in ("app", "master", "hardware", "catalog"):
            self.members.append(_Member(label, classify_member(path),
                                        lambda p=path: open(p, "rb"), os.path.getsize(path)))
        else:
            raise IngestError("unsupported input (need a directory, a zip/.knxprod or an "
                              "application XML): %s" % path)

    def _add_dir(self, root, depth):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                kind = classify_member(fn)
                rel = os.path.relpath(full, root)
                if kind == "container":
                    if zipfile.is_zipfile(full):
                        self._add_zip(full, depth + 1, rel)
                elif kind:
                    self.members.append(_Member(rel, kind, lambda p=full: open(p, "rb"),
                                                os.path.getsize(full)))

    def _add_zip(self, path, depth, label):
        if depth > self.max_depth:
            raise IngestError("zip nesting deeper than %d: %s" % (self.max_depth, label))
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as e:
            raise IngestError("bad zip %s: %s" % (label, e))
        self._zips.append(zf)
        infos = zf.infolist()
        self._count += len(infos)
        if self._count > self.max_members:
            raise IngestError("too many zip members (> %d)" % self.max_members)
        for info in infos:
            if not safe_member_name(info.filename):
                raise IngestError("unsafe member name in %s: %r" % (label, info.filename))
        read = [i for i in infos if not i.is_dir() and classify_member(i.filename)]
        for i in read:
            if i.file_size and (not i.compress_size or
                                i.file_size > ZIP_MAX_RATIO * i.compress_size):
                raise IngestError("member %s of %s expands more than %d:1: not product data"
                                  % (i.filename, label, ZIP_MAX_RATIO))
        self._total += sum(i.file_size for i in read)
        if self._total > self.max_total:
            raise IngestError("uncompressed size of the product data exceeds %d MB "
                              "(KNXPROD_ZIP_MAX_MB)" % (self.max_total >> 20))
        for info in infos:
            if info.is_dir():
                continue
            kind = classify_member(info.filename)
            if kind is None:
                continue
            name = "%s!%s" % (label, info.filename)
            if info.flag_bits & 0x1:
                self.warnings.append("encrypted member skipped: %s" % name)
                continue
            if kind == "container":
                os.makedirs(self.tmp_dir, exist_ok=True)
                tmp = os.path.join(self.tmp_dir, "nested-%d-%d-%d.zip" % (
                    os.getpid(), threading.get_ident(), len(self._tmpfiles)))
                self._tmpfiles.append(tmp)
                written = 0
                with zf.open(info) as src, open(tmp, "wb") as dst:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size:
                            raise IngestError("member larger than declared: %s" % name)
                        dst.write(chunk)
                if zipfile.is_zipfile(tmp):
                    self._add_zip(tmp, depth + 1, name)
                else:
                    self.warnings.append("not a zip, skipped: %s" % name)
            else:
                self.members.append(_Member(name, kind, lambda z=zf, i=info: z.open(i),
                                            info.file_size))


class _HashingReader:
    def __init__(self, f):
        self.f = f
        self.h = hashlib.sha256()

    def read(self, n=-1):
        b = self.f.read(n)
        self.h.update(b)
        return b

    def finish(self):
        while True:
            b = self.f.read(1 << 16)
            if not b:
                break
            self.h.update(b)
        return self.h.hexdigest()


# ---------------------------------------------------------------------------
# index database: product files -> application programs
#
# One SQLite file in the data directory, index.sqlite, holds what was
# distilled.  Per program: a header (the record without parameters, com
# objects and types), a list entry, the product files it was found in
# ("sources"), its types, and its parameters and com objects as rows.  A
# program's rows are one contiguous id range in index order, so a question
# reads only that program's rows and only the narrow columns it filters on;
# bodies (compressed JSON) are read for the rows of one reply.  Up to 0.5 the
# index was one JSON file per program, and a program with half a million
# parameters took 8.7 s to load before its first answer.
#
# Programs are keyed by their full id: two programs may report the same
# ApplicationId (manufacturer, number, version), and both are kept.  A file
# that disappears or changes takes its programs with it unless another file
# still provides them.  Writers hold _IngestLock; readers never wait for a
# writer (write-ahead log).  The data directory belongs on a local disk:
# SQLite's locking does not work over network file systems.
# ---------------------------------------------------------------------------

class _IngestLock:
    """Serialises all writes to a data directory: re-entrant within a thread,
    exclusive between threads (RLock) and between processes (flock)."""
    _rlock = threading.RLock()
    _depth = 0
    _fd = None

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, ".lock")

    def __enter__(self):
        cls = _IngestLock
        cls._rlock.acquire()
        try:
            if cls._depth == 0:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                cls._fd = fd
            cls._depth += 1
        except BaseException:
            cls._rlock.release()
            raise
        return self

    def __exit__(self, *exc):
        cls = _IngestLock
        try:
            cls._depth -= 1
            if cls._depth == 0:
                fcntl.flock(cls._fd, fcntl.LOCK_UN)
                os.close(cls._fd)
                cls._fd = None
        finally:
            cls._rlock.release()


def _index_entry(rec):
    e = {"app_id": rec["app_id"], "aliases": rec.get("aliases", []), "id": rec["id"],
         "name": rec["name"], "manufacturer": rec["manufacturer"].get("name"),
         "manufacturer_id": rec["manufacturer"].get("id"),
         "application_number": rec["application_number"],
         "application_version": rec["application_version"], "mask": rec["mask"].get("id"),
         "order_numbers": [p["order_number"] for p in rec["products"] if p.get("order_number")],
         "product_texts": [p["text"] for p in rec["products"] if p.get("text")],
         "comobjects": rec["counts"]["comobjects"], "parameters": rec["counts"]["parameters"],
         "converted": bool(rec.get("converted_from_pre_ets4")),
         "product_count": len(rec["products"])}
    om = rec.get("original_manufacturer")
    if om:
        e["original_manufacturer"] = om.get("name")
        e["original_manufacturer_id"] = om.get("id")
    return e


_DB_SCHEMA = (
    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE files(rel TEXT PRIMARY KEY, sha256 TEXT, size INTEGER, mtime_ns INTEGER, "
    "status TEXT NOT NULL, info TEXT NOT NULL, reingest INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE programs(pk INTEGER PRIMARY KEY, full_id TEXT NOT NULL UNIQUE COLLATE NOCASE, "
    "app_id TEXT NOT NULL, written_by TEXT NOT NULL, p_lo INTEGER, p_hi INTEGER, "
    "c_lo INTEGER, c_hi INTEGER, max_nbytes INTEGER NOT NULL, layout TEXT NOT NULL, "
    "header BLOB NOT NULL, entry TEXT NOT NULL)",
    "CREATE INDEX programs_app ON programs(app_id)",
    "CREATE TABLE sources(pk INTEGER NOT NULL, rel TEXT NOT NULL, PRIMARY KEY(pk, rel)) "
    "WITHOUT ROWID",
    "CREATE INDEX sources_rel ON sources(rel)",
    "CREATE TABLE aliases(alias TEXT NOT NULL, pk INTEGER NOT NULL, PRIMARY KEY(alias, pk)) "
    "WITHOUT ROWID",
    "CREATE TABLE types(pk INTEGER NOT NULL, id TEXT NOT NULL, body BLOB NOT NULL, "
    "PRIMARY KEY(pk, id))",
    "CREATE TABLE params(id INTEGER PRIMARY KEY, pk INTEGER NOT NULL, storage TEXT, "
    "segment TEXT, address INTEGER, seg_offset INTEGER, size_bits INTEGER, nbytes INTEGER, "
    "module_id TEXT, module_label TEXT, search TEXT NOT NULL)",
    "CREATE INDEX params_address ON params(pk, address) WHERE address IS NOT NULL",
    "CREATE INDEX params_segment ON params(pk, segment, seg_offset) WHERE segment IS NOT NULL",
    "CREATE TABLE param_bodies(id INTEGER PRIMARY KEY, body BLOB NOT NULL)",
    "CREATE TABLE comobjects(id INTEGER PRIMARY KEY, pk INTEGER NOT NULL, number INTEGER, "
    "module_id TEXT, module_label TEXT, search TEXT NOT NULL)",
    "CREATE INDEX comobjects_number ON comobjects(pk, number)",
    "CREATE TABLE comobject_bodies(id INTEGER PRIMARY KEY, body BLOB NOT NULL)",
)
_ROW_COLUMNS = {
    "params": ("storage", "segment", "address", "seg_offset", "size_bits", "nbytes",
               "module_id", "module_label", "search"),
    "comobjects": ("number", "module_id", "module_label", "search"),
}
_BODIES = {"params": "param_bodies", "comobjects": "comobject_bodies"}
LEGACY_INDEX = ("index.json", "files.json", "master.json", "apps")    # the JSON index of 0.5


def _db_path(data_dir):
    return os.path.join(data_dir, DB_NAME)


def _open_writer(data_dir, log=None):
    """The index database for writing, the caller holding _IngestLock.  It is
    created when missing and emptied when it is in another format (the JSON
    index of 0.5 is removed then), so that the sync rebuilds it from the
    product files.  Returns (connection, the old format or None)."""
    os.makedirs(data_dir, exist_ok=True)
    con = sqlite3.connect(_db_path(data_dir), timeout=60, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA journal_size_limit=%d" % (64 << 20))
        old = _ensure_schema_locked(con, data_dir, log)
    except BaseException:
        con.close()
        raise
    return con, old


def _ensure_schema_locked(con, data_dir, log=None):
    version = con.execute("PRAGMA user_version").fetchone()[0]
    legacy = [n for n in LEGACY_INDEX if os.path.exists(os.path.join(data_dir, n))]
    if version == SCHEMA_VERSION and not legacy:
        return None
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite%'")]
    old = None
    if legacy:
        old = (_load_json(os.path.join(data_dir, "index.json")) or {}).get("schema") or "unknown"
    elif tables:
        old = version or "unknown"
    con.execute("BEGIN IMMEDIATE")
    try:
        for t in tables:
            con.execute('DROP TABLE "%s"' % t)
        for stmt in _DB_SCHEMA:
            con.execute(stmt)
        con.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    if tables:
        con.execute("VACUUM")               # give the old rows' space back
    for n in legacy:
        full = os.path.join(data_dir, n)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            try:
                os.remove(full)
            except FileNotFoundError:
                pass
    if old is not None and log:
        log("%s index format %s, this version writes %s: rebuilding the index from the "
            "product files" % (_utc_now(), old, SCHEMA_VERSION))
    return old


def _touch_generation(con):
    """Readers cache the program list until this number changes."""
    con.execute("INSERT INTO meta(key, value) VALUES ('generation', '1') ON CONFLICT(key) "
                "DO UPDATE SET value = CAST(value AS INTEGER) + 1")


def _delete_program_rows_locked(con, pk):
    """A program's rows, types and aliases; the program itself stays."""
    r = con.execute("SELECT p_lo, p_hi, c_lo, c_hi FROM programs WHERE pk = ?", (pk,)).fetchone()
    if r is None:
        return
    for table, lo, hi in (("params", r[0], r[1]), ("comobjects", r[2], r[3])):
        if lo is not None:
            con.execute("DELETE FROM %s WHERE id BETWEEN ? AND ?" % table, (lo, hi))
            con.execute("DELETE FROM %s WHERE id BETWEEN ? AND ?" % _BODIES[table], (lo, hi))
    con.execute("DELETE FROM types WHERE pk = ?", (pk,))
    con.execute("DELETE FROM aliases WHERE pk = ?", (pk,))


def _insert_rows_locked(con, table, pk, rows):
    """A program's rows get consecutive ids above every id in use."""
    if not rows:
        return None, None
    lo = con.execute("SELECT IFNULL(MAX(id), 0) + 1 FROM %s" % table).fetchone()[0]
    cols = _ROW_COLUMNS[table]
    con.executemany("INSERT INTO %s(id, pk, %s) VALUES (?, ?%s)" % (
        table, ", ".join(cols), ", ?" * len(cols)),
        ((lo + i, pk) + r[:-1] for i, r in enumerate(rows)))
    con.executemany("INSERT INTO %s(id, body) VALUES (?, ?)" % _BODIES[table],
                    ((lo + i, r[-1]) for i, r in enumerate(rows)))
    return lo, lo + len(rows) - 1


def _store_program_locked(con, rec, sink, source):
    """Write one distilled program; an earlier one with the same full id is
    replaced and keeps its other sources.  Returns (pk, it existed before)."""
    params, cos = sink.ordered()
    header = {k: v for k, v in rec.items() if k not in ("parameters", "comobjects", "types")}
    vals = (rec["id"], rec["app_id"], source, sink.max_nbytes,
            sink.fingerprint(rec.get("byte_order", {}).get("used")), _pack(header),
            _dumps(_index_entry(rec)))
    old = con.execute("SELECT pk FROM programs WHERE full_id = ?", (rec["id"],)).fetchone()
    if old:
        pk = old[0]
        _delete_program_rows_locked(con, pk)
        con.execute("UPDATE programs SET full_id = ?, app_id = ?, written_by = ?, max_nbytes = ?, "
                    "layout = ?, header = ?, entry = ? WHERE pk = ?", vals + (pk,))
    else:
        pk = con.execute("INSERT INTO programs(full_id, app_id, written_by, max_nbytes, layout, "
                         "header, entry) VALUES (?, ?, ?, ?, ?, ?, ?)", vals).lastrowid
    p_lo, p_hi = _insert_rows_locked(con, "params", pk, params)
    c_lo, c_hi = _insert_rows_locked(con, "comobjects", pk, cos)
    con.execute("UPDATE programs SET p_lo = ?, p_hi = ?, c_lo = ?, c_hi = ? WHERE pk = ?",
                (p_lo, p_hi, c_lo, c_hi, pk))
    con.executemany("INSERT INTO types(pk, id, body) VALUES (?, ?, ?)",
                    ((pk, tid, _pack(t)) for tid, t in rec.get("types", {}).items()))
    con.executemany("INSERT OR IGNORE INTO aliases(alias, pk) VALUES (?, ?)",
                    ((a, pk) for a in rec.get("aliases", [])))
    con.execute("INSERT OR IGNORE INTO sources(pk, rel) VALUES (?, ?)", (pk, source))
    return pk, bool(old)


def index_record(data_dir, rec, source="record"):
    """Write one distilled record (a dict as build_record returns it without
    `rows`) to the index: for tests and tools that build records themselves."""
    sink = _RowSink()
    for p in rec.get("parameters", []):
        sink.add("parameter", p)
    for c in rec.get("comobjects", []):
        sink.add("comobject", c)
    with _IngestLock(data_dir):
        con, _ = _open_writer(data_dir)
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                pk, _ = _store_program_locked(con, rec, sink, source)
                _touch_generation(con)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()
    return pk


def _drop_source_locked(con, source, pks):
    """Remove `source` from these programs.  Returns the app ids of the
    programs left without any source (deleted) and the other sources whose
    files must be read again because the dropped file had written them."""
    removed, stale = set(), set()
    for pk in sorted(pks):
        row = con.execute("SELECT app_id, written_by FROM programs WHERE pk = ?", (pk,)).fetchone()
        con.execute("DELETE FROM sources WHERE pk = ? AND rel = ?", (pk, source))
        if row is None:
            continue
        rest = sorted(r[0] for r in con.execute("SELECT rel FROM sources WHERE pk = ?", (pk,)))
        if not rest:
            _delete_program_rows_locked(con, pk)
            con.execute("DELETE FROM programs WHERE pk = ?", (pk,))
            removed.add(row[0])
        elif row[1] == source:
            stale.add(rest[0])
    return sorted(removed), sorted(stale)


def _remove_source_locked(con, source):
    """Product file `source` no longer provides anything."""
    con.execute("BEGIN IMMEDIATE")
    try:
        pks = [r[0] for r in con.execute("SELECT pk FROM sources WHERE rel = ?", (source,))]
        removed, stale = _drop_source_locked(con, source, pks)
        if pks:
            _touch_generation(con)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return removed, stale


def _ingest_locked(con, path, data_dir, source, label=None, log=None):
    """Distill every application program in `path` and record it as coming
    from product file `source`.  Programs `source` provided before but no
    longer contains lose it; those left without any source are removed.  Each
    program is committed on its own, so a manufacturer's whole database never
    sits in the write-ahead log at once.  Raises IngestError when nothing
    usable is inside."""
    t0 = time.monotonic()
    tmp = os.path.join(data_dir, "tmp")
    os.makedirs(tmp, exist_ok=True)
    row = con.execute("SELECT value FROM meta WHERE key = 'master'").fetchone()
    master = json.loads(row[0]) if row else {"manufacturers": {}, "masks": {}}
    warnings, written, per_app, product_ids = [], {}, [], set()
    fresh, pairs = set(), set()
    with SourceSet(path, tmp, label=label or source) as src:
        warnings.extend(src.warnings)
        kinds = collections.Counter(m.kind for m in src.members)
        if not kinds.get("app"):
            raise IngestError("no application program XML (M-xxxx_A-....xml) in %s" % source)
        if not kinds.get("master") and not master.get("manufacturers"):
            warnings.append("no knx_master.xml: manufacturer names and mask procedures unknown")
        if not kinds.get("hardware"):
            warnings.append("no Hardware.xml: products not linked from this file")
        for m in src.members:
            if m.kind == "master":
                with m.open() as f:
                    new = parse_master_xml(f)
                master["manufacturers"].update(new["manufacturers"])
                master["masks"].update(new["masks"])
                master["version"] = new.get("version") or master.get("version")
        hw = {"programs": {}, "products": {}, "langs": {}, "h2p": {}}
        catalog = []
        for m in src.members:
            if m.kind == "hardware":
                with m.open() as f:
                    new = parse_hardware_xml(f)
                for k, lst in new["programs"].items():
                    dst = hw["programs"].setdefault(k, [])
                    dst.extend(x for x in lst if x not in dst)
                hw["products"].update(new["products"])
                hw["h2p"].update(new["h2p"])
                for lang, d in new["langs"].items():
                    hw["langs"].setdefault(lang, {}).update(d)
            elif m.kind == "catalog":
                try:
                    with m.open() as f:
                        catalog.extend(parse_catalog_xml(f))
                except ET.ParseError as e:
                    warnings.append("XML error, skipped %s: %s" % (m.name, e))
        # The catalog names the program each product is sold with; linking per
        # hardware gave every version of a program all products of its hardware.
        by_app = {}
        for prod, h2p in catalog:
            for app in hw["h2p"].get(h2p, []):
                lst = by_app.setdefault(app, [])
                if prod not in lst:
                    lst.append(prod)
        hw["programs"].update(by_app)
        seen_xml = set()
        for m in src.members:
            if m.kind != "app":
                continue
            with m.open() as f:
                hr = _HashingReader(f)
                try:
                    raws, langs = parse_application_xml(hr)
                except ET.ParseError as e:
                    warnings.append("XML error, skipped %s: %s" % (m.name, e))
                    continue
                digest = hr.finish()
            if digest in seen_xml:
                # The same program XML once more: manufacturer bundles repeat it
                # in several nested archives.  Nothing new, nothing to warn about.
                continue
            seen_xml.add(digest)
            for raw in raws:
                sink = _RowSink()
                try:
                    rec = build_record(raw, langs, master, hw,
                                       {"member": m.name, "sha256": digest,
                                        "ingested": _utc_now()}, rows=sink)
                except Exception as e:      # one broken program must not stop the rest
                    warnings.append("skipped %s in %s: %s: %s" % (
                        raw.full_id, m.name, type(e).__name__, e))
                    continue
                app_id, full_id = rec["app_id"], rec["id"]
                key = full_id.upper()
                if key in written:
                    warnings.append("%s defined twice with different content; the later one "
                                    "wins" % full_id)
                old = con.execute("SELECT header FROM programs WHERE full_id = ?",
                                  (full_id,)).fetchone()
                if not rec["products"] and not kinds.get("hardware") and old:
                    prev = _unpack(old[0])
                    if prev.get("products"):
                        rec["products"] = prev["products"]
                # Two programs may report the same ApplicationId.  Where their
                # layouts differ, a device cannot tell them apart, only the
                # product it was sold as can.
                layout = sink.fingerprint(rec["byte_order"].get("used"))
                twins = [r[0] for r in con.execute(
                    "SELECT full_id FROM programs WHERE app_id = ? AND full_id != ? AND "
                    "layout != ? ORDER BY full_id", (app_id, full_id, layout))
                    if frozenset((r[0].upper(), key)) not in pairs]
                if twins:
                    pairs.update(frozenset((t.upper(), key)) for t in twins)
                    warnings.append("%s and %s report the same ApplicationId %s with different "
                                    "memory layouts or com objects: both are kept, the product "
                                    "installed decides which one a device runs"
                                    % (full_id, ", ".join(twins[:3]), app_id))
                con.execute("BEGIN IMMEDIATE")
                try:
                    pk, existed = _store_program_locked(con, rec, sink, source)
                    _touch_generation(con)
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
                del sink
                if not existed:
                    fresh.add(app_id)
                written[key] = (pk, app_id)
                product_ids.update(pr["order_number"] for pr in rec["products"])
                c = rec["counts"]
                pa = {"app_id": app_id, "id": full_id, "name": rec["name"],
                      "products": len(rec["products"]),
                      "comobjects": c["comobjects"],
                      "comobject_refs": c["comobject_refs"],
                      "parameters": c["parameters"],
                      "parameters_memory": c["parameters_memory"],
                      "parameters_union": c["parameters_union"],
                      "template_check": c["template_check"],
                      "byte_order": rec["byte_order"]}
                for k in ("module_instances", "module_parameters", "module_comobjects",
                          "module_unsupported"):
                    if c.get(k):
                        pa[k] = c[k]
                per_app.append(pa)
                if rec["byte_order"].get("warning"):
                    warnings.append("%s: %s" % (app_id, rec["byte_order"]["warning"]))
                if c.get("module_unsupported"):
                    warnings.append("%s: ETS module features not resolved: %s" % (
                        app_id, ", ".join("%s=%d" % kv for kv in c["module_unsupported"].items())))
    if not written:
        raise IngestError("no application program could be distilled from %s" % source)
    con.execute("BEGIN IMMEDIATE")
    try:
        previous = {r[0] for r in con.execute("SELECT pk FROM sources WHERE rel = ?", (source,))}
        removed, stale = _drop_source_locked(con, source,
                                             previous - {pk for pk, _ in written.values()})
        con.execute("INSERT INTO meta(key, value) VALUES ('master', ?) ON CONFLICT(key) "
                    "DO UPDATE SET value = excluded.value", (_dumps(master),))
        _touch_generation(con)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    ev = collections.Counter()
    for pa in per_app:
        ev.update(pa["byte_order"]["template_evidence"])
        ev.update(pa["byte_order"]["bit_order_evidence"])
    summary = {
        "source": source, "apps_added": sorted({a for _, a in written.values()}),
        "apps_new": sorted(fresh),
        "apps_removed": removed, "stale_sources": stale,
        "apps_total": con.execute("SELECT count(*) FROM programs").fetchone()[0],
        "products": len(product_ids),
        "comobjects": sum(pa["comobjects"] for pa in per_app),
        "comobject_refs": sum(pa["comobject_refs"] for pa in per_app),
        "parameters": sum(pa["parameters"] for pa in per_app),
        "parameters_memory": sum(pa["parameters_memory"] for pa in per_app),
        "parameters_union": sum(pa["parameters_union"] for pa in per_app),
        "template_check": {"checked": sum(pa["template_check"]["checked"] for pa in per_app),
                           "match": sum(pa["template_check"]["match"] for pa in per_app)},
        "layout_evidence": {k: ev[k] for k in ("msb_first", "lsb_first", "big", "little")},
        "warnings": warnings, "seconds": round(time.monotonic() - t0, 2), "per_app": per_app}
    if log:
        log("%s indexed %s: %d application program(s), %d product(s), %d warning(s), %.1f s"
            % (_utc_now(), source, len(written), len(product_ids), len(warnings),
               summary["seconds"]))
    return summary


def ingest_file(path, data_dir, source=None, log=None):
    """Index one product file (or an extracted directory) as `source`."""
    data_dir = os.path.abspath(data_dir)
    with _IngestLock(data_dir):
        con, _ = _open_writer(data_dir, log)
        try:
            return _ingest_locked(con, path, data_dir,
                                  source or os.path.basename(path.rstrip("/")), log=log)
        finally:
            con.close()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def is_product_file(name):
    base = name.rsplit("/", 1)[-1]
    return (not base.startswith(".") and base.lower().endswith(PRODUCT_EXTENSIONS))


def product_files(products_dir):
    """{relative path: (full path, stat)} of every product file, recursively.
    Hidden files and directories are skipped (uploads in progress are hidden)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(products_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if not is_product_file(fn):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                out[os.path.relpath(full, products_dir).replace(os.sep, "/")] = (full, st)
    return out


def _file_info(summary):
    return {"apps": summary["apps_added"], "ingested": _utc_now(),
            "warnings": summary["warnings"][:20]}


def _put_file_locked(con, rel, sha, st, status, info):
    con.execute("INSERT INTO files(rel, sha256, size, mtime_ns, status, info, reingest) "
                "VALUES (?, ?, ?, ?, ?, ?, 0) ON CONFLICT(rel) DO UPDATE SET "
                "sha256 = excluded.sha256, size = excluded.size, mtime_ns = excluded.mtime_ns, "
                "status = excluded.status, info = excluded.info, reingest = 0",
                (rel, sha, st.st_size, st.st_mtime_ns, status, _dumps(info)))


def _stats(db):
    """Programs, product files and files that could not be read, from a
    _Reader."""
    files = db.rows("SELECT count(*), IFNULL(SUM(status = 'error'), 0) FROM files")
    return {"apps_total": db.one("SELECT count(*) FROM programs", default=0),
            "files_total": files[0][0] if files else 0,
            "file_errors": files[0][1] if files else 0}


def sync_products(products_dir, data_dir, settle=SETTLE_SECONDS, log=None):
    """Bring the index in line with the product directory: index new and
    changed files, drop the applications of files that are gone.  A file is
    read only once it has not changed for `settle` seconds; one whose size
    and mtime are unchanged is not even hashed.  A missing product directory
    (not mounted yet?) changes nothing; an empty one empties the index.  A
    scan that finds nothing to do writes nothing."""
    t0 = time.monotonic()
    products_dir = os.path.abspath(products_dir)
    data_dir = os.path.abspath(data_dir)
    summary = {"added": [], "changed": [], "refreshed": [], "removed": [], "unchanged": 0,
               "pending": [], "errors": {}, "apps_removed": [], "missing": False}
    if not os.path.isdir(products_dir):
        summary["missing"] = True
        con = _open_reader(_db_path(data_dir))
        try:
            summary.update(_stats(_Reader(con)))
        finally:
            if con is not None:
                con.close()
        summary["seconds"] = round(time.monotonic() - t0, 2)
        return summary
    with _IngestLock(data_dir):
        con, old_schema = _open_writer(data_dir, log)
        try:
            if old_schema is not None:
                summary["rebuilt_from_schema"] = old_schema
            _sync_locked(con, products_dir, data_dir, settle, log, summary)
            summary.update(_stats(_Reader(con)))
        finally:
            con.close()
    summary["seconds"] = round(time.monotonic() - t0, 2)
    return summary


def _sync_locked(con, products_dir, data_dir, settle, log, summary):
    files = {r[0]: {"sha256": r[1], "size": r[2], "mtime_ns": r[3], "reingest": r[4]}
             for r in con.execute("SELECT rel, sha256, size, mtime_ns, reingest FROM files")}
    present = product_files(products_dir)
    known = set(files) | {r[0] for r in con.execute("SELECT DISTINCT rel FROM sources")}
    force = {rel for rel, r in files.items() if r["reingest"]}
    todo = []

    def queue(rels):
        for rel in rels:
            if rel in present and rel not in force:
                force.add(rel)
                todo.append(rel)

    # files that are gone
    for rel in sorted(known - set(present)):
        removed, stale = _remove_source_locked(con, rel)
        con.execute("DELETE FROM files WHERE rel = ?", (rel,))
        files.pop(rel, None)
        summary["removed"].append(rel)
        summary["apps_removed"] += removed
        force.update(s for s in stale if s in present)
        if log:
            log("%s removed %s: %d application program(s) dropped"
                % (_utc_now(), rel, len(removed)))
    # new and changed files
    now = time.time()
    for rel, (full, st) in sorted(present.items()):
        r = files.get(rel)
        if (r and rel not in force and r["size"] == st.st_size
                and r["mtime_ns"] == st.st_mtime_ns):
            summary["unchanged"] += 1
            continue
        if settle and now - st.st_mtime < settle:
            summary["pending"].append(rel)
            continue
        todo.append(rel)
    while todo:
        rel = todo.pop(0)
        full, st = present[rel]
        try:
            st = os.stat(full)
            sha = _sha256_file(full)
        except OSError as e:
            summary["errors"][rel] = str(e)[:300]
            continue
        r = files.get(rel)
        if r and r["sha256"] == sha and rel not in force:
            con.execute("UPDATE files SET size = ?, mtime_ns = ? WHERE rel = ?",
                        (st.st_size, st.st_mtime_ns, rel))           # touched, same bytes
            summary["unchanged"] += 1
            continue
        force.discard(rel)
        try:
            res = _ingest_locked(con, full, data_dir, rel, log=log)
        except (IngestError, zipfile.BadZipFile, ET.ParseError, OSError) as e:
            removed, stale = _remove_source_locked(con, rel)
            _put_file_locked(con, rel, sha, st, "error",
                             {"error": str(e)[:300], "apps": [], "checked": _utc_now()})
            files[rel] = {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                          "reingest": 0}
            summary["errors"][rel] = str(e)[:300]
            summary["apps_removed"] += removed
            queue(stale)
            if log:
                log("%s cannot index %s: %s" % (_utc_now(), rel, str(e)[:300]))
            continue
        kind = "added" if not r else ("refreshed" if r["sha256"] == sha else "changed")
        summary[kind].append(rel)
        _put_file_locked(con, rel, sha, st, "ok", _file_info(res))
        files[rel] = {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                      "reingest": 0}
        summary["apps_removed"] += res["apps_removed"]
        queue(res["stale_sources"])
    for rel in force:                   # stale but not readable this round
        if rel in files and not files[rel]["reingest"]:
            con.execute("UPDATE files SET reingest = 1 WHERE rel = ?", (rel,))


def _upload_name(raw):
    """Sanitised file name for an upload: no directories, no hidden names, a
    product extension."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")[:120]
    if not name:
        name = "upload-%s.zip" % datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
    if not name.lower().endswith(PRODUCT_EXTENSIONS):
        name += ".zip"
    return name


def store_upload(products_dir, data_dir, tmp_path, name, sha):
    """Index an uploaded product file (already written to `tmp_path` inside
    the product directory) and give it its final name there.  A different file
    with the same name is never overwritten: the new one gets a suffix.  An
    identical file that is already indexed is not stored twice."""
    products_dir = os.path.abspath(products_dir)
    data_dir = os.path.abspath(data_dir)
    with _IngestLock(data_dir):
        con, _ = _open_writer(data_dir)     # a new format: the next scan reads the rest again
        try:
            for rel, info in con.execute("SELECT rel, info FROM files WHERE sha256 = ? AND "
                                         "status = 'ok' ORDER BY rel", (sha,)).fetchall():
                if os.path.isfile(os.path.join(products_dir, rel)):
                    os.remove(tmp_path)
                    return {"stored": False, "duplicate_of": rel, "file": rel,
                            "apps_added": json.loads(info).get("apps", []), "apps_new": [],
                            "apps_removed": [], "warnings": []}
            stem, ext = name[:-len(name.rsplit(".", 1)[-1]) - 1], "." + name.rsplit(".", 1)[-1]
            candidate, n, existing = name, 1, False
            while os.path.exists(os.path.join(products_dir, candidate)):
                if _sha256_file(os.path.join(products_dir, candidate)) == sha:
                    existing = True             # same bytes, not indexed yet: use that file
                    break
                n += 1
                candidate = "%s-%d%s" % (stem, n, ext)
            res = _ingest_locked(con, tmp_path, data_dir, candidate, label=candidate)
            final = os.path.join(products_dir, candidate)
            if existing:
                os.remove(tmp_path)
            else:
                os.replace(tmp_path, final)
                _fsync_dir(products_dir)
            _put_file_locked(con, candidate, sha, os.stat(final), "ok", _file_info(res))
            for rel in res["stale_sources"]:    # cannot happen for a new name; keep it consistent
                con.execute("UPDATE files SET reingest = 1 WHERE rel = ?", (rel,))
        finally:
            con.close()
    out = dict(res)
    out.update({"stored": not existing, "file": candidate})
    if candidate != name:
        out["renamed_from"] = name
    return out


def _prepare_dirs(products_dir, data_dir):
    """Create the directories; fail with a clear message if the index cannot
    be written.  A read-only product directory is fine (uploads then fail)."""
    tmp = os.path.join(data_dir, "tmp")
    try:
        for d in (data_dir, tmp):
            os.makedirs(d, exist_ok=True)
        probe = os.path.join(data_dir, ".write-test-%d" % os.getpid())
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
    except OSError as e:
        raise ConfigError(
            "data directory %s is not writable for uid %d / gid %d (%s). Create it as the "
            "user the server runs as, or change its owner." % (
                data_dir, os.getuid(), os.getgid(), e.strerror or e))
    if not os.path.isdir(products_dir):
        try:
            os.makedirs(products_dir, exist_ok=True)
        except OSError as e:
            raise ConfigError("product directory %s does not exist and cannot be created (%s)"
                              % (products_dir, e.strerror or e))
    old = time.time() - 600
    for d, prefix in ((products_dir, ".upload-"), (tmp, "nested-")):
        try:
            for fn in os.listdir(d):
                full = os.path.join(d, fn)
                if fn.startswith(prefix) and os.path.getmtime(full) < old:
                    os.remove(full)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# data access for the server
# ---------------------------------------------------------------------------

def _open_reader(path):
    """A connection for reading, None while the index does not exist."""
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=rw" % quote(path), uri=True, timeout=30,
                              isolation_level=None, check_same_thread=False)
        con.execute("PRAGMA query_only = 1")
    except sqlite3.Error:
        return None
    return con


class _Reader:
    """The queries of one request, on one connection and in one read
    transaction: a sync committing meanwhile is not seen in halves."""

    def __init__(self, con):
        self.con = con

    def rows(self, sql, args=()):
        if self.con is None:
            return []
        try:
            return self.con.execute(sql, args).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):       # created, not set up yet
                return []
            raise

    def one(self, sql, args=(), default=None):
        r = self.rows(sql, args)
        return r[0][0] if r and r[0][0] is not None else default

    def bodies(self, table, ids):
        """The bodies of these rows, in this order."""
        got = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            got.update(self.rows("SELECT id, body FROM %s WHERE id IN (%s)" % (
                _BODIES[table], ",".join("?" * len(chunk))), chunk))
        return [_unpack(got[i]) for i in ids if i in got]

    def page(self, table, where, args, offset, limit):
        """(number of rows matching, bodies of the matching rows offset ..
        offset + limit in index order)."""
        total = self.one("SELECT count(*) FROM %s WHERE %s" % (table, where), args, 0)
        if offset >= total:
            return total, []
        ids = [r[0] for r in self.rows("SELECT id FROM %s WHERE %s ORDER BY id LIMIT ? OFFSET ?"
                                       % (table, where), list(args) + [limit, offset])]
        return total, self.bodies(table, ids)

    def types(self, pk, ids):
        """The types of one program with these ids."""
        ids = sorted({i for i in ids if i})
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for tid, body in self.rows("SELECT id, body FROM types WHERE pk = ? AND id IN (%s)"
                                       % ",".join("?" * len(chunk)), [pk] + chunk):
                out[tid] = _unpack(body)
        return out

    def programs(self, app_id, full_id=None):
        """The program with this full id; without one, the programs reporting
        this app id (or, if none does, having it as an OEM alias)."""
        cols = "pk, full_id, app_id, p_lo, p_hi, c_lo, c_hi, max_nbytes, layout, header"
        if full_id:
            rows = self.rows("SELECT %s FROM programs WHERE full_id = ?" % cols, (full_id,))
        else:
            rows = self.rows("SELECT %s FROM programs WHERE app_id = ? ORDER BY full_id" % cols,
                             (app_id,))
            if not rows:
                rows = self.rows("SELECT %s FROM programs WHERE pk IN (SELECT pk FROM aliases "
                                 "WHERE alias = ?) ORDER BY full_id" % cols, (app_id,))
        return [_Program(self, r) for r in rows]


class _Program:
    """An application program as the tools see it: its header (the record
    without parameters, com objects and types), its product files, and where
    its rows are."""

    def __init__(self, db, row):
        (self.pk, self.full_id, self.app_id, self.p_lo, self.p_hi, self.c_lo, self.c_hi,
         self.max_nbytes, self.layout, header) = row
        self.header = _unpack(header)
        self.equivalent = []
        self.sources = [r[0] for r in db.rows("SELECT rel FROM sources WHERE pk = ? ORDER BY rel",
                                              (self.pk,))]


def _preference(converted, products, full_id):
    """Order among programs of one ApplicationId and one layout: native ETS4
    data before data converted from ETS3, more products first, then by id."""
    return (bool(converted), -(products or 0), full_id)


def resolve_program(db, app_id, full_id=None):
    """The program an app id (or full id) stands for: (program, []).  Several
    programs of one layout are one answer, the preferred one, with the others
    in program.equivalent.  (None, programs) when programs of different
    layouts report the app id; (None, []) when none does."""
    progs = db.programs(app_id, full_id)
    if not progs:
        return None, []
    if len({p.layout for p in progs}) > 1:
        return None, progs
    prog = min(progs, key=lambda p: _preference(p.header.get("converted_from_pre_ets4"),
                                                len(p.header.get("products") or []), p.full_id))
    prog.equivalent = [r[0] for r in db.rows(
        "SELECT full_id FROM programs WHERE app_id = ? AND layout = ? AND pk != ? "
        "ORDER BY full_id", (prog.app_id, prog.layout, prog.pk))]
    return prog, []


class Store:
    """Read access to the index for the server: pooled connections, and the
    program list cached until the index changes."""

    def __init__(self, data_dir):
        self.data_dir = os.path.abspath(data_dir)
        self.path = _db_path(self.data_dir)
        self._lock = threading.Lock()
        self._pool = []
        self._entries = (None, [])

    @contextlib.contextmanager
    def reader(self):
        with self._lock:
            con = self._pool.pop() if self._pool else None
        if con is None:
            con = _open_reader(self.path)
        if con is not None:
            con.execute("BEGIN")
        try:
            yield _Reader(con)
        finally:
            if con is not None:
                try:
                    con.execute("COMMIT")
                except sqlite3.Error:
                    con.close()
                    con = None
            if con is not None:
                with self._lock:
                    if len(self._pool) < 8:
                        self._pool.append(con)
                        con = None
                if con is not None:
                    con.close()

    def close(self):
        with self._lock:
            pool, self._pool = self._pool, []
        for con in pool:
            con.close()

    def entries(self):
        """List entries in app_id order, each with its pk and product files
        ("sources", "written_by" the one read last): one per app id whose
        programs share a layout ("equivalent" counts the others), every
        program with "shared_app_id" where layouts differ.  Read again only
        after the index changed."""
        with self.reader() as db:
            gen = db.one("SELECT value FROM meta WHERE key = 'generation'", default="0")
            with self._lock:
                if self._entries[0] == gen:
                    return self._entries[1]
            out = []
            for pk, written_by, layout, entry in db.rows(
                    "SELECT pk, written_by, layout, entry FROM programs ORDER BY app_id, full_id"):
                e = json.loads(entry)
                e["pk"], e["written_by"], e["layout"] = pk, written_by, layout
                out.append(e)
            srcs = collections.defaultdict(list)
            for pk, rel in db.rows("SELECT pk, rel FROM sources ORDER BY rel"):
                srcs[pk].append(rel)
        # One entry per app id where its programs share a layout (the one the
        # tools answer from); where layouts differ, every program with its id.
        groups = collections.OrderedDict()
        for e in out:
            e["sources"] = srcs.get(e["pk"], [])
            groups.setdefault(e["app_id"], []).append(e)
        listed = []
        for es in groups.values():
            if len({e["layout"] for e in es}) == 1:
                best = min(es, key=lambda e: _preference(e.get("converted"),
                                                         e.get("product_count"), e["id"]))
                best["equivalent"] = len(es) - 1
                listed.append(best)
            else:
                for e in es:
                    e["shared_app_id"] = True
                    listed.append(e)
        with self._lock:
            self._entries = (gen, listed)
        return listed

    def stats(self):
        with self.reader() as db:
            return _stats(db)

    def files(self):
        """The product files known to the index: rel -> record."""
        out = {}
        with self.reader() as db:
            for rel, sha, size, mtime, status, info, reingest in db.rows(
                    "SELECT rel, sha256, size, mtime_ns, status, info, reingest FROM files"):
                r = dict(json.loads(info), sha256=sha, size=size, mtime_ns=mtime, status=status)
                if reingest:
                    r["reingest"] = True
                out[rel] = r
        return out

    def app_ids(self):
        return sorted({e["app_id"] for e in self.entries()})

    def app(self, app_id):
        """The whole record of one program as one dict (tests, export), by app
        id or full id, resolved as the tools resolve it; None when it is
        unknown or ambiguous."""
        with self.reader() as db:
            prog, _ = resolve_program(db, normalize_app_id(app_id), normalize_program_id(app_id))
            if prog is None:
                return None
            rec = dict(prog.header)
            rec["types"] = {tid: _unpack(body) for tid, body in db.rows(
                "SELECT id, body FROM types WHERE pk = ? ORDER BY rowid", (prog.pk,))}
            for key, table, lo, hi in (("comobjects", "comobjects", prog.c_lo, prog.c_hi),
                                       ("parameters", "params", prog.p_lo, prog.p_hi)):
                rec[key] = db.bodies(table, list(range(lo, hi + 1))) if lo is not None else []
        return rec


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

class ToolError(Exception):
    def __init__(self, error, message, **extra):
        super().__init__(message)
        self.payload = dict(error=error, message=message, **extra)


def _arg_str(args, key, maxlen=200):
    v = args.get(key)
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        raise ToolError("invalid_argument", "%s must be a string" % key)
    v = v.strip()
    if len(v) > maxlen:
        raise ToolError("invalid_argument", "%s is longer than %d characters" % (key, maxlen))
    return v or None


def _arg_int(args, key, default, lo, hi):
    v = args.get(key)
    if v is None:
        return default
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    elif isinstance(v, str) and re.match(r"^\s*\d+\s*$", v):
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ToolError("invalid_argument", "%s must be an integer" % key)
    if v < lo or v > hi:
        raise ToolError("invalid_argument", "%s must be between %d and %d" % (key, lo, hi))
    return v


def _arg_bool(args, key, default=False):
    v = args.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true"
    raise ToolError("invalid_argument", "%s must be true or false" % key)


def parse_address(v, key="address"):
    """int, '0x4A2F', '4A2Fh' or a decimal string -> int."""
    a = None
    if isinstance(v, int) and not isinstance(v, bool):
        a = v
    elif isinstance(v, str):
        s = v.strip().lower().replace("_", "")
        try:
            if s.startswith("0x"):
                a = int(s[2:], 16)
            elif re.match(r"^[0-9a-f]+h$", s):
                a = int(s[:-1], 16)
            elif re.match(r"^\d+$", s):
                a = int(s)
        except ValueError:
            a = None
    if a is None or not 0 <= a <= 0xFFFFFFFF:
        raise ToolError("invalid_argument",
                        "%s must be an integer or a string like '0x4A2F' (hex) or '18991'" % key)
    return a


def _words_match(query, fields):
    hay = " ".join(f for f in fields if f).lower()
    return all(w in hay for w in query.lower().split())


def _segment_arg(rec, args):
    """The 'segment' argument as the program's segment id, None if absent."""
    seg = _arg_str(args, "segment")
    if seg is None:
        return None
    ids = [s["id"] for s in rec.get("segments", [])]
    for i in ids:
        if i.lower() == seg.lower():
            return i
    raise ToolError("invalid_argument", "%s is not a segment of this program" % seg,
                    segments=ids[:20])


def _relative_hint(rec):
    rel = [s["id"] for s in rec.get("segments", []) if s.get("relative")]
    if not rel:
        return None
    return ("parameters of this program live in relative segment %s: pass segment and the "
            "offset within it as address" % " / ".join(rel[:4]))


def need_app(db, args):
    """The program `app_id` names: the ApplicationId a device reports, or a
    program's full id where several programs report the same ApplicationId."""
    raw = args.get("app_id")
    app_id = normalize_app_id(raw)
    if app_id is None:
        raise ToolError("invalid_argument",
                        "app_id must be 10 hex digits: manufacturer (4), application number (4), "
                        "version (2), e.g. FFF0123456", app_id=raw)
    full_id = normalize_program_id(raw)
    prog, progs = resolve_program(db, app_id, full_id)
    if prog is not None:
        return prog
    if progs:
        # A device reports the same ApplicationId for each of them, and their
        # layouts differ: picking one would answer with the memory layout of a
        # program the device may not run.
        raise ToolError(
            "ambiguous_app_id",
            "%d application programs with different layouts report %s: a device cannot tell "
            "them apart, the product it was sold as can" % (len(progs), app_id), app_id=app_id,
            programs=[{"id": p.full_id, "name": p.header.get("name"),
                       "order_numbers": [x.get("order_number")
                                         for x in p.header.get("products", [])][:6],
                       "product_files": p.sources[:2]} for p in progs[:10]],
            hint="ask which product is installed and pass its program's id as app_id; where "
                 "one product is listed under several programs, the installation's ETS project "
                 "names the program loaded")
    cands = []
    for k, full, entry in db.rows("SELECT app_id, full_id, entry FROM programs "
                                  "ORDER BY app_id, full_id"):
        e = json.loads(entry)
        ids = [k] + list(e.get("aliases", []))
        if any(i[4:8] == app_id[4:8] and (i[:4] == app_id[:4] or i[8:] == app_id[8:]) for i in ids):
            cands.append({"app_id": k, "id": full, "name": e.get("name"),
                          "version_hex": "%02X" % e.get("application_version", 0)})
    extra = {}
    if cands:
        extra["note"] = ("candidates are other versions of this program: objects, variants and "
                         "memory layout can differ, do not answer from them in place of the "
                         "device's own version")
    raise ToolError("unknown_app_id", "application program %s is not ingested"
                    % (full_id or app_id), app_id=full_id or app_id, candidates=cands[:10],
                    hint="prod-applications lists what is ingested", **extra)


def _mfr_match(e, wanted):
    w = wanted.strip()
    wl = w.lower()
    ids = [e.get("manufacturer_id") or "", e.get("original_manufacturer_id") or ""]
    names = [e.get("manufacturer") or "", e.get("original_manufacturer") or ""]
    h = wl[2:] if wl.startswith("m-") or wl.startswith("0x") else wl
    for i in ids:
        if i and i[2:].lower() == h.zfill(4)[-4:] and re.match(r"^[0-9a-f]{1,4}$", h):
            return True
    if re.match(r"^\d+$", wl):
        for i in ids:
            if i and int(i[2:], 16) == int(wl):
                return True
    return any(wl in n.lower() for n in names if n)


def _page(window, total, offset, render, header, key, budget, notes=None, max_level=3):
    """Serialise `window`, the items from `offset` on (at most one page of the
    `total` that match); return fewer when the reply would exceed the byte
    budget.  Detail levels drop optional fields only when even a single item
    does not fit, and a reply that used one says so in `detail` (notes[level])
    — it used to drop conditions without a word."""
    level_now = [0]

    def build(rendered, n):
        o = dict(header)
        if level_now[0] and notes:
            o["detail"] = notes.get(level_now[0])
        o["total"] = total
        o["offset"] = offset
        o["returned"] = n
        o["next_offset"] = offset + n if offset + n < total else None
        o[key] = rendered[:n]
        return _dumps(o)

    if not window:
        return build([], 0)
    for level in range(max_level + 1):
        level_now[0] = level
        rendered = [render(x, level) for x in window]
        acc = _esc_len(build([], 0)) + 24
        n = 0
        for r in rendered:
            s = _esc_len(_dumps(r)) + 1
            if acc + s > budget:
                break
            acc += s
            n += 1
        while n > 0:
            text = build(rendered, n)
            if _esc_len(text) <= budget:
                return text
            n -= 1
    raise ToolError("result_too_large", "a single entry does not fit into %d bytes" % budget)


def _fit_single(build, levels, budget):
    for level in range(levels):
        text = _dumps(build(level))
        if _esc_len(text) <= budget:
            return text
    raise ToolError("result_too_large", "the summary does not fit into %d bytes" % budget)


def _render_app_entry(e, level):
    o = {"app_id": e["app_id"]}
    if e.get("shared_app_id"):
        o["id"] = e["id"]
    o.update({"name": e.get("name"), "manufacturer": e.get("manufacturer"),
              "version": e.get("application_version"),
              "version_hex": "%02X" % (e.get("application_version") or 0), "mask": e.get("mask"),
              "products": len(e.get("order_numbers", []))})
    if level < 2:
        o["order_numbers"] = e.get("order_numbers", [])[:6 if level == 0 else 3]
    if e.get("aliases"):
        o["aliases"] = e["aliases"]
    if e.get("original_manufacturer") and level == 0:
        o["original_manufacturer"] = e["original_manufacturer"]
    return o


def tool_applications(store, args, budget):
    entries = store.entries()
    mfr = _arg_str(args, "manufacturer")
    query = _arg_str(args, "query")
    offset = _arg_int(args, "offset", 0, 0, 1 << 30)
    limit = _arg_int(args, "limit", 40, 1, 200)
    items = []
    for e in entries:
        if mfr and not _mfr_match(e, mfr):
            continue
        if query and not _words_match(query, [e.get("name"), e.get("id"), e["app_id"]]
                                      + e.get("aliases", []) + e.get("order_numbers", [])
                                      + e.get("product_texts", [])):
            continue
        items.append(e)
    window = items[offset:offset + limit]
    header = {"ingested": store.stats()["apps_total"]}
    if not entries:
        header["note"] = "nothing ingested yet"
    elif any(e.get("shared_app_id") for e in window):
        header["note"] = ("an entry with an id shares its app_id with another program: a device "
                          "reports the same ApplicationId for both; pass the id as app_id")
    return _page(window, len(items), offset, _render_app_entry, header, "applications", budget)


def tool_application(store, args, budget):
    with store.reader() as db:
        prog = need_app(db, args)
    rec = prog.header
    caps = [(None, None, None), (20, 40, 32), (8, 20, 16), (3, 0, 8)]

    def build(level):
        cap_p, cap_s, cap_seg = caps[level]
        o = {"app_id": rec["app_id"]}
        if rec.get("aliases"):
            o["aliases"] = rec["aliases"]
        o.update({"id": rec["id"], "name": rec["name"]})
        if rec.get("name_de") and level < 2:
            o["name_de"] = rec["name_de"]
        o["manufacturer"] = rec["manufacturer"]
        if rec.get("original_manufacturer"):
            o["original_manufacturer"] = rec["original_manufacturer"]
        for k in ("application_number", "application_number_hex", "application_version",
                  "application_version_hex", "program_type", "mask", "load_procedure_style",
                  "converted_from_pre_ets4"):
            o[k] = rec.get(k)
        prods = rec.get("products", [])
        if cap_p is not None and len(prods) > cap_p:
            o["products"] = prods[:cap_p]
            o["products_total"] = len(prods)
        else:
            o["products"] = prods
        segs = rec.get("segments", [])
        o["segments"] = segs if cap_seg is None else segs[:cap_seg]
        if cap_seg is not None and len(segs) > cap_seg:
            o["segments_total"] = len(segs)
        o["tables"] = rec.get("tables", {})
        o["byte_order"] = rec.get("byte_order", {})
        o["counts"] = rec.get("counts", {})
        mods = rec.get("modules")
        if mods:
            cap_m = (None, 16, 8, 3)[level]
            o["modules"] = mods if cap_m is None else mods[:cap_m]
            if cap_m is not None and len(mods) > cap_m:
                o["modules_total"] = len(mods)
        lp = rec.get("load_procedure", {})
        steps = lp.get("steps", [])
        o["load_procedure"] = {"source": lp.get("source"),
                               "steps": steps if cap_s is None else steps[:cap_s]}
        if cap_s is not None and len(steps) > cap_s:
            o["load_procedure"]["steps_total"] = len(steps)
        if prog.sources:
            o["product_files"] = prog.sources[:8 if cap_p is None else 3]
        if prog.equivalent:
            o["equivalent_programs"] = prog.equivalent[:8 if cap_p is None else 3]
        o["note"] = SLOT_NOTE
        return o

    return _fit_single(build, len(caps), budget)


def _put_when(dst, src, level):
    """when / when_any into a reply: in full at detail level 0, from level 1 on
    only the innermost condition of each (the reply is over its budget)."""
    if src.get("when"):
        dst["when"] = src["when"] if level == 0 else src["when"][-1:]
    if src.get("when_any"):
        dst["when_any"] = src["when_any"] if level == 0 else _uniq(a[-1:] for a in src["when_any"])
        if src.get("when_any_more"):
            dst["when_any_more"] = True
    if src.get("when_more"):
        dst["when_more"] = True


def _variant_groups(c):
    """One entry per distinct size/flags/text of an object's variants, with
    their refs: an overview for objects with very many variants."""
    groups = collections.OrderedDict()
    for v in c.get("variants", []):
        key = (v.get("size_bits"), v.get("flags"), v.get("text"), v.get("function_text"),
               v.get("dpt"))
        g = groups.get(key)
        if g is None:
            g = {"size_bits": v.get("size_bits"), "flags": v.get("flags")}
            if v.get("text") != c["text"]:
                g["text"] = v.get("text")
            if v.get("function_text") and v.get("function_text") != c.get("function_text"):
                g["function_text"] = v["function_text"]
            if v.get("dpt") and v.get("dpt") != c.get("dpt"):
                g["dpt"] = v["dpt"]
            g["count"] = 0
            g["refs"] = []
            groups[key] = g
        g["count"] += 1
        if len(g["refs"]) < 24:
            g["refs"].append(v["ref"])
    return list(groups.values())


def _render_co_variant(v, c, level):
    """A variant always keeps its full conditions: they are what tells two
    variants of the same size apart."""
    w = {"ref": v["ref"], "size_bits": v.get("size_bits"), "flags": v.get("flags")}
    if v.get("size") != c.get("size"):
        w["size"] = v.get("size")
    if v.get("text") != c["text"]:
        w["text"] = v.get("text")
        if level == 0 and v.get("text_de"):
            w["text_de"] = v["text_de"]
    if v.get("function_text") and v.get("function_text") != c.get("function_text"):
        w["function_text"] = v["function_text"]
    if v.get("dpt") and v.get("dpt") != c.get("dpt"):
        w["dpt"] = v["dpt"]
    if v.get("priority") and v.get("priority") != c.get("priority"):
        w["priority"] = v["priority"]
    _put_when(w, v, 0)
    if level == 0 and v.get("block"):
        w["block"] = v["block"]
    if level == 0 and v.get("seen"):
        w["seen"] = v["seen"]
    if v.get("ui") is False:
        w["ui"] = False
    return w


def _render_comobject(c, level, variants=None):
    o = {"number": c["number"], "text": c["text"]}
    if c.get("module"):
        o["module"] = c["module"]["label"]
    if level == 0 and c.get("text_de"):
        o["text_de"] = c["text_de"]
    if c.get("function_text"):
        o["function_text"] = c["function_text"]
    if level == 0 and c.get("function_text_de"):
        o["function_text_de"] = c["function_text_de"]
    if level == 0 and c.get("name"):
        o["name"] = c["name"]
    o["size"] = c.get("size")
    o["size_bits"] = c.get("size_bits")
    o["flags"] = c.get("flags")
    if c.get("priority") and c["priority"] != "Low":
        o["priority"] = c["priority"]
    if c.get("dpt"):
        o["dpt"] = c["dpt"]
    o["variants"] = [_render_co_variant(v, c, level)
                     for v in (c.get("variants", []) if variants is None else variants)]
    return o


_CO_NOTES = {1: "reduced: no text_de/name/block"}


def _page_variants(c, total, offset, voff, header, budget):
    """One object whose variants do not fit into one reply, in parts: as many
    variants as fit, each with its full conditions, and where to go on.  `c` is
    the object at `offset`, None past the end.  The first part adds an overview
    by size/flags/text.  It replaced an emergency level that cut every
    variant's conditions to the innermost one, after which variants of the
    same size could no longer be told apart."""
    if c is None:
        o = dict(header, total=total, offset=offset, returned=0, next_offset=None, comobjects=[])
        return _dumps(o)
    vs = c.get("variants", [])
    if voff >= len(vs) and vs:
        raise ToolError("invalid_argument", "variant_offset %d: object %s has %d variants"
                        % (voff, c["number"], len(vs)))

    def build(k, groups):
        o = _render_comobject(c, 1, vs[voff:voff + k])
        nxt = voff + k if voff + k < len(vs) else None
        o.update(variants_total=len(vs), variants_offset=voff, variants_next_offset=nxt)
        if groups:
            o["variant_groups"] = _variant_groups(c)
        # next_offset always moves on: a client that only follows it must not
        # be sent back to the same object for ever (it was, until a real-data
        # run paging every program never finished).
        h = dict(header, total=total, offset=offset, returned=1,
                 next_offset=offset + 1 if offset + 1 < total else None)
        if nxt is not None:
            h["detail"] = ("object %s has more variants than one reply holds: ask again with "
                           "offset %d and variant_offset %d for the next ones"
                           % (c["number"], offset, nxt))
        else:
            h["detail"] = "the last variants of object %s" % c["number"]
        h["comobjects"] = [o]
        return _dumps(h)

    # The overview only where it condenses: one group per variant says nothing.
    condenses = voff == 0 and len(_variant_groups(c)) * 2 <= len(vs)
    for groups in ((True, False) if condenses else (False,)):
        k = len(vs) - voff
        while k > 0:
            text = build(k, groups)
            if _esc_len(text) <= budget:
                return text
            k -= max(1, k // 8)
    raise ToolError("result_too_large", "a single variant does not fit into %d bytes" % budget)


def _module_where(w, a, wanted):
    """Module instance by label ('Channel B') or id: 'MD-1_M-2' is that
    instance (with its repetitions), 'MD-1' every instance of definition MD-1."""
    m = wanted.strip().lower()
    w.append("(module_label = ? OR module_id = ? OR substr(module_id, 1, ?) = ?)")
    a += [m, m, len(m) + 1, m + "_"]


def _query_where(w, a, query):
    """Every word somewhere in the row's texts, case-insensitive."""
    for word in query.lower().split():
        w.append("instr(search, ?) > 0")
        a.append(word)


def tool_comobjects(store, args, budget):
    with store.reader() as db:
        prog = need_app(db, args)
        rec = prog.header
        numbers = args.get("numbers")
        if numbers is not None:
            if isinstance(numbers, int) and not isinstance(numbers, bool):
                numbers = [numbers]
            if not isinstance(numbers, list) or len(numbers) > 512 or not all(
                    isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 0xFFFF
                    for n in numbers):
                raise ToolError("invalid_argument",
                                "numbers must be a list of up to 512 integers 0..65535")
        query = _arg_str(args, "query")
        module = _arg_str(args, "module")
        offset = _arg_int(args, "offset", 0, 0, 1 << 30)
        limit = _arg_int(args, "limit", 40, 1, 200)
        voff = _arg_int(args, "variant_offset", 0, 0, 1 << 30)
        if numbers is not None:
            wanted = sorted(set(numbers))
            w = ["pk = ?", "number IN (%s)" % ",".join("?" * len(wanted)) if wanted else "0"]
            a = [prog.pk] + wanted
        else:
            wanted = None
            w, a = ["id BETWEEN ? AND ?"], [prog.c_lo, prog.c_hi]
        if module:
            _module_where(w, a, module)
        if query:
            _query_where(w, a, query)
        where = " AND ".join(w)
        header = {"app_id": rec["app_id"], "name": rec["name"],
                  "note": SLOT_NOTE + " " + VARIANT_NOTE}
        if wanted is not None:
            found = {r[0] for r in db.rows("SELECT DISTINCT number FROM comobjects WHERE "
                                           + where, a)}
            missing = [n for n in wanted if n not in found]
            if missing:
                header["missing_numbers"] = missing[:64]
        total, window = db.page("comobjects", where, a, offset, 1 if voff else limit)
    if voff == 0:
        try:
            return _page(window, total, offset, _render_comobject, header, "comobjects", budget,
                         notes=_CO_NOTES, max_level=1)
        except ToolError as e:
            if e.payload.get("error") != "result_too_large":
                raise
    return _page_variants(window[0] if window else None, total, offset, voff, header, budget)


def _param_nbytes(p):
    size = p.get("size_bits") or 1
    return (p.get("bit_offset", 0) + size + 7) // 8


def _render_type(ty, level):
    if not ty:
        return None
    o = {"kind": ty.get("kind")}
    if ty.get("kind") in ("number", "float"):
        for k in ("min", "max"):
            if ty.get(k) is not None:
                o[k] = ty[k]
        if ty.get("signed"):
            o["signed"] = True
        if ty.get("encoding"):
            o["encoding"] = ty["encoding"]
    elif ty.get("kind") == "color" and ty.get("space"):
        o["space"] = ty["space"]
    elif ty.get("kind") == "enum":
        vals = ty.get("values", [])
        cap = (64, 24, 8, 4)[level]
        o["values"] = vals[:cap]
        if len(vals) > cap:
            o["values_total"] = len(vals)
        if ty.get("binary"):
            o["binary"] = ty["binary"][:cap]
    return o


def _binary_enum(ty, v, size):
    """(value, hex) of an enum stored as BinaryValue bytes; value None if no match."""
    h = "%0*X" % ((size + 3) // 4, v)
    for b, value in ty.get("binary", []):
        if b == h:
            return value, h
    return None, h


def _enum_text(ty, value):
    if ty and ty.get("kind") == "enum" and value is not None:
        for v, t in ty.get("values", []):
            if v == value:
                return t
    return None


_PARAM_NOTES = {1: "reduced: no text_de/name/block, enum values <= 24",
                2: "reduced: conditions cut to the innermost one, enum values <= 8",
                3: "reduced: no conditions, enum values <= 4"}


def _render_param_variant(v, level):
    """A ref that differs from the parameter, with the conditions that show
    it — kept at every level, it is the per-mode default or access."""
    w = {k: v[k] for k in ("ref", "default", "access", "ui") if k in v}
    if v.get("text") and level < 2:
        w["text"] = v["text"]
    if level < 3:
        _put_when(w, v, 0 if level < 2 else 1)
    return w


def _render_param(p, types, level, order="big"):
    o = {"id": p["id"], "text": p["text"]}
    if p.get("module"):
        o["module"] = p["module"]["label"]
    if level == 0 and p.get("text_de"):
        o["text_de"] = p["text_de"]
    if level == 0 and p.get("name"):
        o["name"] = p["name"]
    size = p.get("size_bits")
    if p.get("storage") == "memory":
        if p.get("address") is not None:
            o["address"] = p["address"]
            o["address_hex"] = "0x%04X" % p["address"]
        else:
            o["segment"] = p.get("segment")
            o["offset"] = p.get("offset")
            if p.get("relative"):
                o["relative_segment"] = True
        o["bit_offset"] = p.get("bit_offset")
    elif p.get("storage") == "property":
        o["property"] = p.get("property")
    else:
        o["storage"] = None
    if size is not None:
        o["size_bits"] = size
    ty = types.get(p.get("type")) if p.get("type") else None
    if p.get("storage") == "memory" and size:
        o["extract"] = extract_rule(p.get("bit_offset", 0), size, order)
        if ty and ty.get("kind") == "float":
            o["extract"]["decode"] = ty.get("encoding") or "float"
        elif ty and ty.get("kind") == "color":
            o["extract"]["decode"] = ty.get("space") or "color"
    rt = _render_type(ty, level)
    if rt:
        o["type"] = rt
    if p.get("default") is not None:
        o["default"] = p["default"]
        dt = _enum_text(ty, p["default"])
        if dt:
            o["default_text"] = dt
    if p.get("access"):
        o["access"] = p["access"]
    if p.get("union"):
        o["union"] = p["union"]
    if level < 3:
        _put_when(o, p, 0 if level < 2 else 1)
    elif p.get("when_more"):
        o["when_more"] = True
    if level == 0 and p.get("block"):
        o["block"] = p["block"]
    if p.get("ui") is False:
        o["ui"] = False
    if p.get("variants"):
        o["variants"] = [_render_param_variant(v, level) for v in p["variants"]]
        if p.get("variants_total"):
            o["variants_total"] = p["variants_total"]
    return o


def _param_where(prog, segment=None, address=None, module=None, query=None,
                 include_virtual=False):
    """WHERE clause and arguments for the parameters of `prog` that pass the
    filters of prod-parameters.  An address is looked up in the address
    index; everything else reads the program's id range."""
    if address is not None:
        lo = address - prog.max_nbytes + 1
        if segment is None:
            w = ["pk = ?", "address BETWEEN ? AND ?", "address + nbytes > ?"]
            a = [prog.pk, lo, address, address]
        else:
            w = ["pk = ?", "segment = ?", "seg_offset BETWEEN ? AND ?",
                 "seg_offset + nbytes > ?"]
            a = [prog.pk, segment, lo, address, address]
    else:
        w, a = ["id BETWEEN ? AND ?"], [prog.p_lo, prog.p_hi]
        if segment is not None:
            w.append("segment = ?")
            a.append(segment)
    if not include_virtual:
        w.append("storage IS NOT NULL")
    if module:
        _module_where(w, a, module)
    if query:
        _query_where(w, a, query)
    return " AND ".join(w), a


def tool_parameters(store, args, budget):
    with store.reader() as db:
        prog = need_app(db, args)
        rec = prog.header
        query = _arg_str(args, "query")
        module = _arg_str(args, "module")
        segment = _segment_arg(rec, args)
        address = parse_address(args["address"]) if args.get("address") is not None else None
        include_virtual = _arg_bool(args, "include_virtual", False)
        offset = _arg_int(args, "offset", 0, 0, 1 << 30)
        limit = _arg_int(args, "limit", 25, 1, 200)
        where, a = _param_where(prog, segment, address, module, query, include_virtual)
        total, window = db.page("params", where, a, offset, limit)
        types = db.types(prog.pk, [p.get("type") for p in window])
    bo = rec.get("byte_order", {})
    order = bo.get("used", "big")
    header = {"app_id": rec["app_id"], "name": rec["name"],
              "byte_order": order,
              "note": SLOT_NOTE + " bit_offset counts from the MSB; value = (integer of "
                      "extract.bytes bytes at address in byte_order >> shift) & mask. Union "
                      "members share memory: only the one matching the channel mode is valid."}
    if bo.get("warning"):
        header["byte_order_warning"] = bo["warning"]
    if segment is not None:
        header["segment"] = segment
    if address is not None:
        key = "segment_offset" if segment is not None else "address"   # "offset" pages
        header[key] = address
        header[key + "_hex"] = "0x%04X" % address
        if segment is None and not total and _relative_hint(rec):
            header["hint"] = _relative_hint(rec)
    return _page(window, total, offset, lambda p, lv: _render_param(p, types, lv, order),
                 header, "parameters", budget, notes=_PARAM_NOTES)


def tool_decode(store, args, budget):
    with store.reader() as db:
        prog = need_app(db, args)
        rec = prog.header
        if args.get("address") is None:
            raise ToolError("invalid_argument", "address is required")
        segment = _segment_arg(rec, args)
        base = parse_address(args["address"])
        hexdata = args.get("data")
        if not isinstance(hexdata, str):
            raise ToolError("invalid_argument", "data must be a hex string, e.g. '0A1B2C'")
        clean = re.sub(r"[\s:,_-]", "", hexdata)
        if clean.lower().startswith("0x"):
            clean = clean[2:]
        if (not clean or len(clean) % 2 or not re.match(r"^[0-9A-Fa-f]+$", clean)
                or len(clean) > 2048):
            raise ToolError("invalid_argument", "data must be an even number of hex digits "
                                                "(1..1024 bytes)")
        data = bytes.fromhex(clean)
        end = base + len(data)
        offset = _arg_int(args, "offset", 0, 0, 1 << 30)
        limit = _arg_int(args, "limit", 40, 1, 200)
        # every parameter that can overlap the bytes, in index order
        lo = base - prog.max_nbytes + 1
        if segment is None:
            where, a = "pk = ? AND address BETWEEN ? AND ?", [prog.pk, lo, end - 1]
        else:
            where, a = ("pk = ? AND segment = ? AND seg_offset BETWEEN ? AND ?",
                        [prog.pk, segment, lo, end - 1])
        params = db.bodies("params", [r[0] for r in db.rows(
            "SELECT id FROM params WHERE %s ORDER BY id" % where, a)])
        types = db.types(prog.pk, [p.get("type") for p in params])
    bo = rec.get("byte_order", {})
    order = bo.get("used", "big")
    values, partial, skipped = [], [], []
    for p in params:
        a = p.get("address") if segment is None else p.get("offset")
        size = p.get("size_bits")
        ty = types.get(p.get("type")) if p.get("type") else None
        if not size:
            # Its size is not known, so it cannot be cut out of the bytes —
            # but it lives here, and silence would say nothing does.
            if base <= a < end:
                skipped.append({"id": p["id"], "kind": ty.get("kind") if ty else None,
                                "why": "size unknown"})
            continue
        nb = _param_nbytes(p)
        if a + nb <= base or a >= end:
            continue
        if a < base or a + nb > end:
            partial.append(p["id"])
            continue
        o = {"id": p["id"], "text": p["text"]}
        if p.get("module"):
            o["module"] = p["module"]["label"]
        if segment is None:
            o["address_hex"] = "0x%04X" % a
        else:
            o["offset"] = a
        o["bit_offset"] = p.get("bit_offset")
        o["size_bits"] = size
        kind = ty.get("kind") if ty else None
        bit = p.get("bit_offset", 0) or 0
        chunk = data[a - base:a - base + nb]
        v = None
        if kind in ("number", "enum") and size <= 64:
            v = extract_bits(data, a - base, bit, size, order)
        if v is not None:
            if kind == "number" and ty.get("signed") and v >= 1 << (size - 1):
                v -= 1 << size
            o["value"] = v
            if kind == "enum" and ty.get("binary"):
                value, h = _binary_enum(ty, v, size)
                o["value"], o["raw_hex"] = (value if value is not None else v), h
                vt = _enum_text(ty, value) if value is not None else None
                o["value_text"] = vt if vt is not None else "(not an enumerated value)"
            elif kind == "enum":
                vt = _enum_text(ty, str(v))
                o["value_text"] = vt if vt is not None else "(not an enumerated value)"
            if p.get("default") is not None:
                o["default"] = p["default"]
        elif kind == "float" and not bit and size in (16, 32, 64):
            o["value"] = decode_float(chunk, order)
            o["encoding"] = ty.get("encoding")
            if o["value"] is None:
                o["note"] = "0x7FFF: DPT 9 says invalid value"
            if p.get("default") is not None:
                o["default"] = p["default"]
        elif kind == "color" and not bit and size == 24:
            o["value"] = list(chunk)
            o["space"] = ty.get("space")
        else:
            o["raw_hex"] = chunk.hex().upper()
            o["kind"] = kind
        if p.get("union"):
            o["union"] = True
        _put_when(o, p, 0)
        values.append(o)
    header = {"app_id": rec["app_id"]}
    if segment is not None:
        header.update({"segment": segment, "segment_offset": base,
                       "segment_offset_hex": "0x%04X" % base})
    else:
        header.update({"address": base, "address_hex": "0x%04X" % base})
    header.update({"length": len(data), "byte_order": order,
                   "note": "Union members and alternative modules are all decoded: only the one "
                           "whose 'when' (and one of its 'when_any') matches the configured "
                           "channel mode is meaningful. "
                           + SLOT_NOTE})
    if bo.get("warning"):
        header["byte_order_warning"] = bo["warning"]
    if segment is None and not values and not partial and _relative_hint(rec):
        header["hint"] = _relative_hint(rec)
    if partial:
        header["partial"] = partial[:20]
        if len(partial) > 20:
            header["partial_total"] = len(partial)
    if skipped:
        header["skipped"] = skipped[:20]
        if len(skipped) > 20:
            header["skipped_total"] = len(skipped)

    def render(o, level):
        if level == 0:
            return o
        r = {k: v for k, v in o.items() if k not in ("when", "when_any", "when_any_more")}
        if level == 1:
            _put_when(r, o, 1)
        return r

    notes = {1: "reduced: conditions cut to the innermost one", 2: "reduced: no conditions"}
    return _page(values[offset:offset + limit], len(values), offset, render, header, "values",
                 budget, notes=notes, max_level=2)


# Kept short on purpose: gateways that federate these tools merge them into
# their own tools/list, and small devices have small buffers.
_PAGE_PROPS = {
    "offset": {"type": "integer", "minimum": 0, "description": "Start index (default 0)"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 200,
              "description": "Max items; replies may hold fewer"},
}
_APP_ID_PROP = {"type": "string",
                "description": "ApplicationId the device reports, 10 hex digits, e.g. FFF0123456"}
_ADDR_PROP = {"type": ["integer", "string"],
              "description": "Memory address: integer or '0x4A2F'"}
_SEGMENT_PROP = {"type": "string", "description": "Relative segment: address = offset in it"}
_MODULE_PROP = {"type": "string", "description": "Module instance label or id"}

TOOLS = [
    {"name": "prod-applications",
     "description": (
         "List ingested KNX application programs (from .knxprod files): app_id, "
         "manufacturer, name, version, mask, order numbers. Filter by manufacturer (name "
         "or id) and/or query (words in name, order number, product text). Paginated: "
         "offset/limit, reply has total and next_offset (null = end)."),
     "inputSchema": {"type": "object", "properties": dict({
         "manufacturer": {"type": "string"},
         "query": {"type": "string"}}, **_PAGE_PROPS)},
     "annotations": {"readOnlyHint": True}},
    {"name": "prod-application",
     "description": (
         "One application program by app_id: products, mask, memory segments (address, "
         "size, load state machine), group address / association / com-object table "
         "locations, byte order, counts, load procedure. An unknown version returns "
         "candidates."),
     "inputSchema": {"type": "object", "properties": {"app_id": _APP_ID_PROP},
                     "required": ["app_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "prod-comobjects",
     "description": (
         "Com objects of an application program with ALL variants (ComObjectRef). Each "
         "variant: size_bits, flags (C R W T U I, '-' = off), 'when' = ETS condition "
         "(channel mode) that shows it, 'when_any' = one of these must hold too; "
         "text/function_text/dpt only where they differ. A "
         "name labels the SLOT, not the function: in another mode the same number may do "
         "something else (a 'slat position' slot can switch a pump). Pick the variant "
         "whose size_bits matches the size the device reports. Filter: numbers, query, "
         "module (instance of an ETS module). Paginated; very many variants come in "
         "parts (variant_offset)."),
     "inputSchema": {"type": "object", "properties": dict({
         "app_id": _APP_ID_PROP,
         "numbers": {"type": "array", "items": {"type": "integer", "minimum": 0,
                                                "maximum": 65535}},
         "query": {"type": "string"},
         "module": _MODULE_PROP,
         "variant_offset": {"type": "integer", "minimum": 0,
                            "description": "Next part of an object's variants"}},
         **_PAGE_PROPS), "required": ["app_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "prod-parameters",
     "description": (
         "Parameters of an application program with their storage: address (segment base "
         "+ offset), bit_offset (from the MSB), size_bits, extract (read `bytes` bytes at "
         "address in byte_order, >> shift, & mask), type (enum / min-max / text), "
         "default, union, 'when' (channel mode that shows it; 'when_any': one of these "
         "must hold too). Union members share "
         "memory; only the one matching the mode is valid. Names label slots, not "
         "functions. Filter: query (words), module (instance of an ETS module), address "
         "(parameters occupying that byte; with segment: offset in it); include_virtual "
         "adds ETS-only parameters without storage. Paginated."),
     "inputSchema": {"type": "object", "properties": dict({
         "app_id": _APP_ID_PROP,
         "query": {"type": "string"},
         "module": _MODULE_PROP,
         "address": _ADDR_PROP,
         "segment": _SEGMENT_PROP,
         "include_virtual": {"type": "boolean"}}, **_PAGE_PROPS), "required": ["app_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "prod-decode",
     "description": (
         "Decode bytes read from device memory (starting at address, or at an offset "
         "in segment) into parameter values: MSB-first bit offsets, the program's byte "
         "order. Only parameters fully inside the bytes; cut-off ones are listed in "
         "'partial'. All union members and alternative modules are decoded: 'when' and "
         "'when_any' say which applies. Pure computation, no device access. Paginated."),
     "inputSchema": {"type": "object", "properties": dict({
         "app_id": _APP_ID_PROP,
         "address": _ADDR_PROP,
         "segment": _SEGMENT_PROP,
         "data": {"type": "string", "description": "Hex bytes as read, e.g. '0A1B2C'"}},
         **_PAGE_PROPS), "required": ["app_id", "address", "data"]},
     "annotations": {"readOnlyHint": True}},
]

TOOL_FUNCS = {
    "prod-applications": tool_applications,
    "prod-application": tool_application,
    "prod-comobjects": tool_comobjects,
    "prod-parameters": tool_parameters,
    "prod-decode": tool_decode,
}


# ---------------------------------------------------------------------------
# MCP (JSON-RPC 2.0, stateless Streamable HTTP, JSON responses only)
# ---------------------------------------------------------------------------

def read_skill():
    """The text of skill/SKILL.md, None if the file is missing."""
    try:
        with open(SKILL_FILE, "rb") as f:
            return f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def skill_resource():
    text = read_skill()
    if text is None:
        return None
    return {"uri": SKILL_URI, "name": "SKILL.md", "title": "knxprod-mcp skill",
            "description": SKILL_DESCRIPTION, "mimeType": "text/markdown",
            "size": len(text.encode("utf-8"))}


def _rpc_error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _call_budget(mid):
    empty = {"jsonrpc": "2.0", "id": mid,
             "result": {"content": [{"type": "text", "text": ""}], "isError": False}}
    return RESPONSE_BUDGET - len(_dumps(empty).encode("utf-8"))


def handle_rpc(msg, store, notes=None):
    """One JSON-RPC message -> response dict, or None for notifications and
    client responses."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600,
                          "Invalid Request")
    method = msg.get("method")
    if method is None:
        return None                      # a response sent by the client
    mid = msg.get("id")
    if not isinstance(method, str):
        return _rpc_error(mid, -32600, "Invalid Request")
    if notes is not None:
        notes.append(method)
    if "id" not in msg:
        return None                      # notification
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(mid, -32602, "params must be an object")
    if method == "initialize":
        pv = params.get("protocolVersion")
        result = {"protocolVersion": pv if pv in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
                  "capabilities": {"tools": {"listChanged": False},
                                   "resources": {"subscribe": False, "listChanged": False}},
                  "serverInfo": {"name": SERVER_NAME, "version": VERSION},
                  "instructions": INSTRUCTIONS}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "resources/list":
        res = skill_resource()
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": [res] if res else []}}
    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resourceTemplates": []}}
    if method == "resources/read":
        uri = params.get("uri")
        text = read_skill() if uri == SKILL_URI else None
        if text is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32002, "message": "Resource not found", "data": {"uri": uri}}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"contents": [
            {"uri": SKILL_URI, "mimeType": "text/markdown", "text": text}]}}
    if method == "tools/call":
        name = params.get("name")
        func = TOOL_FUNCS.get(name)
        if func is None:
            return _rpc_error(mid, -32602, "Unknown tool: %s" % (name,))
        if notes is not None:
            notes.append(name)
        args = params.get("arguments")
        if args is None:
            args = {}
        is_error = False
        try:
            if not isinstance(args, dict):
                raise ToolError("invalid_argument", "arguments must be an object")
            text = func(store, args, _call_budget(mid))
        except ToolError as e:
            text, is_error = _dumps(e.payload), True
        except Exception as e:                       # pragma: no cover - logged
            traceback.print_exc()
            text = _dumps({"error": "internal_error", "message": str(e)[:300]})
            is_error = True
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}
    return _rpc_error(mid, -32601, "Method not found: %s" % method)


# ---------------------------------------------------------------------------
# mDNS / DNS-SD responder (RFC 6762, RFC 6763), deliberately small
#
# Announces _knxprod-mcp._tcp.local so that clients on the LAN find the server
# without a URL.  It answers PTR (service and service enumeration), SRV, TXT
# and A queries for its own names, honours the QU bit and legacy unicast
# queries, probes its instance name once at start (" (2)" etc. on conflict),
# announces three times and says goodbye (TTL 0) on a clean shutdown.  It
# shares UDP port 5353 with other responders on the host.
# ---------------------------------------------------------------------------

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SERVICE_LABELS = ("_knxprod-mcp", "_tcp", "local")
ENUM_LABELS = ("_services", "_dns-sd", "_udp", "local")
T_A, T_PTR, T_TXT, T_AAAA, T_SRV, T_ANY = 1, 12, 16, 28, 33, 255
C_IN = 1
CACHE_FLUSH = 0x8000
QU_BIT = 0x8000
TTL_HOST = 120                     # SRV, A
TTL_OTHER = 4500                   # PTR, TXT
LEGACY_TTL = 10                    # RFC 6762 6.7: at most 10 s for legacy unicast
FLAGS_RESPONSE = 0x8400            # QR + AA
IFF_UP, IFF_LOOPBACK, IFF_POINTOPOINT, IFF_MULTICAST = 0x1, 0x8, 0x10, 0x1000
_BRIDGE_IF_RE = re.compile(r"^(docker\d*|br-|veth|virbr|lxcbr|lxdbr|cni|podman|flannel|cali|vxlan|kube)")
# default address pools of Docker (bridge networks) and Podman
_CONTAINER_NETS = ([ipaddress.ip_network("172.%d.0.0/16" % i) for i in range(17, 32)]
                   + [ipaddress.ip_network("10.88.0.0/16")])


def _key(labels):
    return tuple(lb.lower() for lb in labels)


def encode_name(labels):
    out = bytearray()
    for lb in labels:
        b = lb.encode("utf-8")
        if not 0 < len(b) <= 63:
            raise ValueError("DNS label must have 1..63 bytes: %r" % lb)
        out.append(len(b))
        out += b
    out.append(0)
    if len(out) > 255:
        raise ValueError("DNS name longer than 255 bytes")
    return bytes(out)


def read_name(data, pos):
    """Decode a (possibly compressed) name at `pos` -> (labels, next position).
    Pointers must point backwards, which rules out loops."""
    labels, end, jumps, total = [], None, 0, 0
    while True:
        if pos >= len(data):
            raise ValueError("name runs past the packet")
        n = data[pos]
        if n & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise ValueError("truncated compression pointer")
            ptr = ((n & 0x3F) << 8) | data[pos + 1]
            if ptr >= pos:
                raise ValueError("compression pointer does not point backwards")
            if end is None:
                end = pos + 2
            jumps += 1
            if jumps > 64:
                raise ValueError("too many compression pointers")
            pos = ptr
            continue
        if n & 0xC0:
            raise ValueError("unsupported label type")
        pos += 1
        if n == 0:
            break
        if pos + n > len(data):
            raise ValueError("label runs past the packet")
        total += n + 1
        if total > 255:
            raise ValueError("name longer than 255 bytes")
        labels.append(bytes(data[pos:pos + n]).decode("utf-8", "replace"))
        pos += n
    return tuple(labels), end if end is not None else pos


def parse_message(data):
    if len(data) < 12:
        raise ValueError("short DNS message")
    mid, flags, qd, an, ns, ar = struct.unpack_from("!6H", data, 0)
    pos, questions, records = 12, [], []
    for _ in range(qd):
        name, pos = read_name(data, pos)
        if pos + 4 > len(data):
            raise ValueError("truncated question")
        qtype, qclass = struct.unpack_from("!2H", data, pos)
        pos += 4
        questions.append((name, qtype, qclass))
    for section, count in (("an", an), ("ns", ns), ("ar", ar)):
        for _ in range(count):
            name, pos = read_name(data, pos)
            if pos + 10 > len(data):
                raise ValueError("truncated record")
            rtype, rclass, ttl, rdlen = struct.unpack_from("!2HIH", data, pos)
            pos += 10
            if pos + rdlen > len(data):
                raise ValueError("truncated record data")
            records.append({"section": section, "name": name, "type": rtype, "class": rclass,
                            "ttl": ttl, "rdata_pos": pos, "rdlen": rdlen})
            pos += rdlen
    return {"id": mid, "flags": flags, "questions": questions, "records": records,
            "data": bytes(data)}


def record_value(msg, r):
    """Comparable form of a record's data (names decompressed)."""
    d, p = msg["data"], r["rdata_pos"]
    if r["type"] == T_PTR:
        return _key(read_name(d, p)[0])
    if r["type"] == T_SRV:
        if r["rdlen"] < 7:
            raise ValueError("short SRV record")
        return struct.unpack_from("!3H", d, p) + (_key(read_name(d, p + 6)[0]),)
    return d[p:p + r["rdlen"]]


def build_message(mid, flags, questions=(), answers=(), authority=(), additionals=()):
    out = bytearray(struct.pack("!6H", mid, flags, len(questions), len(answers),
                                len(authority), len(additionals)))
    for labels, qtype, qclass in questions:
        out += encode_name(labels) + struct.pack("!2H", qtype, qclass)
    for blob in list(answers) + list(authority) + list(additionals):
        out += blob
    return bytes(out)


class _Rec:
    __slots__ = ("labels", "rtype", "ttl", "unique", "rdata", "value")

    def __init__(self, labels, rtype, ttl, unique, rdata, value):
        self.labels, self.rtype, self.ttl = labels, rtype, ttl
        self.unique, self.rdata, self.value = unique, rdata, value

    def wire(self, ttl=None, flush=True):
        rclass = C_IN | (CACHE_FLUSH if self.unique and flush else 0)
        return (encode_name(self.labels) + struct.pack(
            "!2HIH", self.rtype, rclass, self.ttl if ttl is None else ttl, len(self.rdata))
            + self.rdata)


def next_instance_name(name):
    """'x' -> 'x (2)', 'x (2)' -> 'x (3)'; stays within one 63-byte label."""
    m = re.match(r"^(.*) \((\d+)\)$", name)
    base, n = (m.group(1), int(m.group(2)) + 1) if m else (name, 2)
    suffix = " (%d)" % n
    return base.encode("utf-8")[:63 - len(suffix)].decode("utf-8", "ignore") + suffix


def clip_label(text):
    return text.encode("utf-8")[:63].decode("utf-8", "ignore")


class MdnsCore:
    """The records and the answering logic, without any socket."""

    def __init__(self, instance, host, port, addresses, txt):
        self.host, self.port = host, port
        self.addresses = list(addresses)
        self.txt = list(txt)
        self.set_instance(instance)

    def set_instance(self, instance):
        self.instance = clip_label(instance)
        self.instance_labels = (self.instance,) + SERVICE_LABELS
        self.host_labels = (self.host, "local")
        srv = struct.pack("!3H", 0, 0, self.port) + encode_name(self.host_labels)
        txt = b"".join(bytes([len(s.encode("utf-8"))]) + s.encode("utf-8") for s in self.txt)
        self.ptr = _Rec(SERVICE_LABELS, T_PTR, TTL_OTHER, False,
                        encode_name(self.instance_labels), _key(self.instance_labels))
        self.enum = _Rec(ENUM_LABELS, T_PTR, TTL_OTHER, False, encode_name(SERVICE_LABELS),
                         _key(SERVICE_LABELS))
        self.srv = _Rec(self.instance_labels, T_SRV, TTL_HOST, True, srv,
                        (0, 0, self.port, _key(self.host_labels)))
        self.txt_rec = _Rec(self.instance_labels, T_TXT, TTL_OTHER, True, txt, txt)
        self.a = [_Rec(self.host_labels, T_A, TTL_HOST, True, socket.inet_aton(a),
                       socket.inet_aton(a)) for a in self.addresses]

    def records(self):
        return [self.ptr, self.enum, self.srv, self.txt_rec] + self.a

    def announcement(self, ttl=None):
        return build_message(0, FLAGS_RESPONSE, answers=[r.wire(ttl) for r in self.records()])

    def probe(self):
        return build_message(0, 0, questions=[(self.instance_labels, T_ANY, C_IN)],
                             authority=[self.srv.wire(flush=False),
                                        self.txt_rec.wire(flush=False)])

    def _lookup(self, labels, qtype):
        k = _key(labels)
        answers, extra = [], []
        if k == _key(SERVICE_LABELS) and qtype in (T_PTR, T_ANY):
            answers.append(self.ptr)
            extra += [self.srv, self.txt_rec] + self.a
        elif k == _key(ENUM_LABELS) and qtype in (T_PTR, T_ANY):
            answers.append(self.enum)
        elif k == _key(self.instance_labels):
            if qtype in (T_SRV, T_ANY):
                answers.append(self.srv)
                extra += self.a
            if qtype in (T_TXT, T_ANY):
                answers.append(self.txt_rec)
        elif k == _key(self.host_labels) and qtype in (T_A, T_ANY):
            answers += self.a
        return answers, extra

    def respond(self, msg, src):
        """[(packet, destination)] for a parsed query from `src`."""
        if msg["flags"] & 0x8000 or (msg["flags"] >> 11) & 0xF:
            return []                        # responses and non-QUERY opcodes
        known = {}
        for r in msg["records"]:
            if r["section"] == "an":
                try:
                    key = (_key(r["name"]), r["type"], record_value(msg, r))
                except (ValueError, struct.error):
                    continue
                known[key] = max(known.get(key, 0), r["ttl"])
        answers, extra, all_qu = [], [], True
        for labels, qtype, qclass in msg["questions"]:
            if qclass & 0x7FFF not in (C_IN, T_ANY):
                continue
            found, more = self._lookup(labels, qtype)
            found = [rec for rec in found if known.get(
                (_key(rec.labels), rec.rtype, rec.value), -1) < rec.ttl / 2]
            if found:
                all_qu = all_qu and bool(qclass & QU_BIT)
                answers += [rec for rec in found if rec not in answers]
                extra += [rec for rec in more if rec not in extra]
        if not answers:
            return []
        extra = [rec for rec in extra if rec not in answers]
        if src[1] != MDNS_PORT:              # legacy unicast: plain DNS rules
            pkt = build_message(
                msg["id"], FLAGS_RESPONSE, questions=msg["questions"],
                answers=[r.wire(min(r.ttl, LEGACY_TTL), flush=False) for r in answers],
                additionals=[r.wire(min(r.ttl, LEGACY_TTL), flush=False) for r in extra])
            return [(pkt, src)]
        pkt = build_message(0, FLAGS_RESPONSE, answers=[r.wire() for r in answers],
                            additionals=[r.wire() for r in extra])
        return [(pkt, src if all_qu else (MDNS_GROUP, MDNS_PORT))]

    def conflicts(self, msg):
        """True if another responder claims our instance name with other data."""
        if not msg["flags"] & 0x8000:
            return False
        mine = {T_SRV: self.srv.value, T_TXT: self.txt_rec.value}
        key = _key(self.instance_labels)
        for r in msg["records"]:
            if _key(r["name"]) != key or r["ttl"] == 0:
                continue
            try:
                value = record_value(msg, r)
            except (ValueError, struct.error):
                continue
            if mine.get(r["type"]) != value:
                return True
        return False


# -- which address to announce

def ipv4_interfaces():
    """[{name, address, netmask, flags}] of this network namespace via ioctl
    (Linux); an empty list where that is not available."""
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return out
    try:
        size = 40 if struct.calcsize("P") == 8 else 32          # sizeof(struct ifreq)
        buf = array.array("B", bytes(size * 256))
        res = fcntl.ioctl(s.fileno(), 0x8912,                    # SIOCGIFCONF
                          struct.pack("iL", size * 256, buf.buffer_info()[0]))
        length = struct.unpack("iL", res)[0]
        data = buf.tobytes()[:length]
        for off in range(0, length, size):
            name = data[off:off + 16].split(b"\0", 1)[0].decode("ascii", "replace")
            ifreq = struct.pack("256s", name.encode("ascii", "replace")[:15])
            flags = struct.unpack_from("H", fcntl.ioctl(s.fileno(), 0x8913, ifreq), 16)[0]
            try:
                netmask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, ifreq)[20:24])
            except OSError:
                netmask = None
            out.append({"name": name, "address": socket.inet_ntoa(data[off + 20:off + 24]),
                        "netmask": netmask, "flags": flags})
    except (OSError, ValueError, struct.error):
        return out
    finally:
        s.close()
    return out


def default_route_ipv4():
    """Source address the kernel would use for the default route (no packet is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        s.connect(("192.0.2.1", 9))
        a = s.getsockname()[0]
        return None if a.startswith("0.") else a
    except OSError:
        return None
    finally:
        s.close()


def in_container():
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as f:
            c = f.read()
    except OSError:
        return False
    return any(k in c for k in ("docker", "containerd", "kubepods", "libpod"))


BRIDGE_HINT = ("mDNS discovery skipped: only container-internal addresses are visible (%s). "
               "Discovery needs host networking (Docker: network_mode: host or --network host). "
               "Clients then need the URL, or set KNXPROD_MDNS_ADDRESS.")


def mdns_plan(bind, override=None, interfaces=None, default_route=None, container=None):
    """Decide which IPv4 address(es) to announce and on which interface.
    Returns {addresses, interface, netmask, reason, warning}; no addresses
    means skip mDNS (reason says why)."""
    if interfaces is None:
        interfaces = ipv4_interfaces()
    if container is None:
        container = in_container()
    # a container that sees no bridge or veth interfaces runs in a bridge network itself
    bridged_ns = container and not any(_BRIDGE_IF_RE.match(i["name"]) for i in interfaces)

    def internal(addr, ifname=None):
        if ifname and _BRIDGE_IF_RE.match(ifname):
            return True
        ip = ipaddress.ip_address(addr)
        return bridged_ns and any(ip in n for n in _CONTAINER_NETS)

    def iface(addr):
        return next((i for i in interfaces if i["address"] == addr), None)

    def skip(reason, warning=True):
        return {"addresses": [], "interface": None, "netmask": None, "reason": reason,
                "warning": warning}

    def use(addrs):
        i = iface(addrs[0])
        return {"addresses": addrs, "interface": addrs[0], "netmask": i and i["netmask"],
                "reason": None, "warning": False}

    if override:
        addrs = [a for a in override if not ipaddress.ip_address(a).is_loopback]
        return use(addrs) if addrs else skip("KNXPROD_MDNS_ADDRESS holds only loopback addresses")
    b = (bind or "").strip()
    if b.startswith("[") and b.endswith("]"):
        b = b[1:-1]
    ip = _ip(b) if b else None
    if ip is not None and ip.version == 6 and not ip.is_unspecified:
        return skip("listening on an IPv6 address; mDNS here announces IPv4 only")
    if ip is not None and not ip.is_unspecified:
        if ip.is_loopback:
            return skip("listening on loopback only; nothing to announce", warning=False)
        i = iface(str(ip))
        if internal(str(ip), i and i["name"]):
            if container:
                return skip(BRIDGE_HINT % ip)
            return skip("%s belongs to a container or VM bridge (%s); mDNS announces LAN "
                        "addresses only" % (ip, i and i["name"]))
        return use([str(ip)])
    usable = []
    for i in interfaces:
        a = ipaddress.ip_address(i["address"])
        if (i["flags"] & IFF_UP and i["flags"] & IFF_MULTICAST
                and not i["flags"] & (IFF_LOOPBACK | IFF_POINTOPOINT)
                and not a.is_loopback and not a.is_link_local
                and not internal(i["address"], i["name"])):
            usable.append(i)
    if not usable:
        seen = sorted({i["address"] for i in interfaces
                       if not ipaddress.ip_address(i["address"]).is_loopback})
        if container:
            return skip(BRIDGE_HINT % (", ".join(seen) or "none"))
        return skip("no multicast-capable LAN interface with an IPv4 address")
    dr = default_route_ipv4() if default_route is None else default_route
    chosen = next((i for i in usable if i["address"] == dr), usable[0])
    base = chosen["name"].split(":", 1)[0]
    addrs = [i["address"] for i in usable if i["name"].split(":", 1)[0] == base]
    addrs.sort(key=lambda a: a != chosen["address"])
    return {"addresses": addrs, "interface": chosen["address"], "netmask": chosen["netmask"],
            "reason": None, "warning": False}


def default_mdns_hostname():
    h = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname().split(".")[0]).strip("-")
    return h[:63] or "knxprod-mcp"


class MdnsResponder(threading.Thread):
    """Owns the UDP socket: probe, announce, answer, goodbye."""

    def __init__(self, core, plan, log, sock=None):
        super().__init__(name="knxprod-mdns", daemon=True)
        self.core, self.plan, self.log, self.sock = core, plan, log, sock
        self.stop_event = threading.Event()
        self.status = "starting"
        self.announced = False
        self._lock = threading.Lock()
        self._conflict_logged = False
        self._net = None
        if plan.get("netmask"):
            self._net = ipaddress.ip_network("%s/%s" % (plan["interface"], plan["netmask"]),
                                             strict=False)

    def info(self):
        return {"enabled": self.status in ("starting", "probing", "announced"),
                "name": self.core.instance, "address": self.core.addresses[0],
                "status": self.status}

    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(("", MDNS_PORT))
            iface = socket.inet_aton(self.plan["interface"])
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         socket.inet_aton(MDNS_GROUP) + iface)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 255)
        except OSError:
            s.close()
            raise
        return s

    def _send(self, pkt, dest):
        with self._lock:
            if self.sock is not None:
                self.sock.sendto(pkt, dest)

    def _recv(self, timeout):
        self.sock.settimeout(max(0.01, timeout))
        try:
            data, src = self.sock.recvfrom(9000)
        except (socket.timeout, BlockingIOError, InterruptedError):
            return None
        if self._net is not None:
            try:
                if ipaddress.ip_address(src[0]) not in self._net:
                    return None              # only on-link senders are answered
            except ValueError:
                return None
        try:
            return parse_message(data), src
        except (ValueError, struct.error):
            return None

    def probe(self):
        """Claim the instance name: three probes 250 ms apart; on a conflict
        take the next name (' (2)', ' (3)', ...)."""
        self.status = "probing"
        time.sleep(random.uniform(0, 0.25))
        for _ in range(16):
            conflict = False
            for _ in range(3):
                self._send(self.core.probe(), (MDNS_GROUP, MDNS_PORT))
                end = time.monotonic() + 0.25
                while not conflict and not self.stop_event.is_set():
                    left = end - time.monotonic()
                    if left <= 0:
                        break
                    got = self._recv(left)
                    if got and self.core.conflicts(got[0]):
                        conflict = True
                if conflict or self.stop_event.is_set():
                    break
            if not conflict:
                return self.core.instance
            old = self.core.instance
            self.core.set_instance(next_instance_name(old))
            self.log("%s mDNS: %r is taken on the network, trying %r"
                     % (_utc_now(), old, self.core.instance))
        return self.core.instance

    def run(self):
        try:
            if self.sock is None:
                self.sock = self._open()
        except OSError as e:
            self.status = "error"
            self.log("%s mDNS: cannot use UDP port %d (%s); discovery is off"
                     % (_utc_now(), MDNS_PORT, e.strerror or e))
            return
        try:
            self.probe()
            if self.stop_event.is_set():
                return
            due = [time.monotonic() + d for d in (0.0, 1.0, 2.0)]
            while not self.stop_event.is_set():
                now = time.monotonic()
                if due and due[0] <= now:
                    due.pop(0)
                    self._send(self.core.announcement(), (MDNS_GROUP, MDNS_PORT))
                    if not self.announced:
                        self.announced = True
                        self.status = "announced"
                        self.log("%s mDNS: announcing %r (%s.local -> %s, port %d)" % (
                            _utc_now(), self.core.instance, self.core.host,
                            ", ".join(self.core.addresses), self.core.port))
                    continue
                got = self._recv(min(1.0, due[0] - now) if due else 1.0)
                if not got:
                    continue
                msg, src = got
                if msg["flags"] & 0x8000:
                    if not self._conflict_logged and self.core.conflicts(msg):
                        self._conflict_logged = True
                        self.log("%s mDNS: another responder (%s) claims %r with other data"
                                 % (_utc_now(), src[0], self.core.instance))
                    continue
                for pkt, dest in self.core.respond(msg, src):
                    self._send(pkt, dest)
        except OSError as e:
            if not self.stop_event.is_set():
                self.status = "error"
                self.log("%s mDNS: stopped after a socket error (%s)"
                         % (_utc_now(), e.strerror or e))

    def stop(self):
        """Stop answering; send a goodbye (TTL 0) if we announced anything."""
        self.stop_event.set()
        if self.is_alive():
            self.join(3)
        try:
            if self.announced:
                self._send(self.core.announcement(ttl=0), (MDNS_GROUP, MDNS_PORT))
        except OSError:
            pass
        with self._lock:
            if self.sock is not None:
                self.sock.close()
                self.sock = None
        if self.status != "error":
            self.status = "stopped"


# ---------------------------------------------------------------------------
# HTTP: Host and Origin guards
#
# Host header (DNS rebinding guard): IP literals, localhost and *.local names
# are accepted, plus the names an operator configures; every other name is
# refused.  A browser Origin on /mcp must pass the same test.  Uploads from a
# browser (CORS) are accepted from private-network origins only.
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    pass


_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_UPLOAD_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",      # RFC 1918
    "fc00::/7", "fe80::/10",                               # IPv6 ULA, link-local
    "127.0.0.0/8", "::1/128")]                             # loopback = localhost
_UPLOAD_TYPES = ("", "application/zip", "application/octet-stream",
                 "application/x-zip-compressed", "application/x-zip")


def split_host_port(value):
    """'name', 'name:port', '1.2.3.4:port', '[v6]', '[v6]:port' or a bare IPv6
    literal -> (host, port or None); (None, None) when malformed."""
    v = (value or "").strip()
    if not v:
        return None, None
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return None, None
        host, rest = v[1:end], v[end + 1:]
        if rest and not re.match(r"^:\d{1,5}$", rest):
            return None, None
        return host, int(rest[1:]) if rest else None
    if v.count(":") == 1:
        host, port = v.split(":")
        if not re.match(r"^\d{1,5}$", port):
            return None, None
        return host, int(port)
    return v, None


def _ip(host):
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _dns_name(host):
    """Lower-case DNS name without trailing dot, or None if not a valid name."""
    name = host.lower()
    if name.endswith("."):
        name = name[:-1]
    if not name or len(name) > 253 or not all(_LABEL_RE.match(lb) for lb in name.split(".")):
        return None
    return name


def normalize_host_entry(entry):
    """Configured extra host name -> canonical form (port and case dropped)."""
    host, _port = split_host_port(entry)
    name = _dns_name(host) if host else None
    if name is None and (host is None or _ip(host) is None):
        raise ConfigError("not a host name: %r" % entry)
    return name or str(_ip(host))


def host_allowed(value, extra=()):
    host, port = split_host_port(value)
    if host is None or (port is not None and not 0 < port < 65536):
        return False
    if _ip(host) is not None:
        return True
    name = _dns_name(host)
    if name is None:
        return False
    return name == "localhost" or name.endswith(".local") or name in extra


def parse_origin(origin):
    """'http(s)://host[:port]' -> (scheme, host, port or None); None otherwise.
    Default ports are dropped so that equal origins compare equal."""
    o = (origin or "").strip()
    m = re.match(r"^(https?)://([^/?#@]+)/?$", o, re.I)
    if not m:
        return None
    scheme = m.group(1).lower()
    host, port = split_host_port(m.group(2))
    if host is None or (port is not None and not 0 < port < 65536):
        return None
    ip = _ip(host)
    if ip is not None:
        host = str(ip)
    else:
        host = _dns_name(host)
        if host is None:
            return None
    if port == {"http": 80, "https": 443}[scheme]:
        port = None
    return scheme, host, port


def mcp_origin_allowed(origin, extra=()):
    o = parse_origin(origin)
    if o is None:
        return False
    host = o[1]
    return host_allowed("[%s]" % host if ":" in host else host, extra)


def upload_origin_allowed(origin, override=None):
    o = parse_origin(origin)
    if o is None:
        return False
    if override is not None:
        return o in override
    ip = _ip(o[1])
    if ip is not None:
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip.version == n.version and ip in n for n in _UPLOAD_NETS)
    return o[1] == "localhost" or o[1].endswith(".local")


def parse_upload_origins(values):
    """Configured origins -> frozenset; None = default rule; 'none' = no
    browser uploads at all."""
    if values is None:
        return None
    vals = [v.strip() for v in values if v and v.strip()]
    if not vals:
        return None
    if [v.lower() for v in vals] == ["none"]:
        return frozenset()
    out = set()
    for v in vals:
        o = parse_origin(v)
        if o is None:
            raise ConfigError("not an origin (scheme://host[:port]): %r" % v)
        out.add(o)
    return frozenset(out)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _HttpError(Exception):
    def __init__(self, status, error, close=False):
        super().__init__(error)
        self.status, self.error, self.close = status, error, close


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "knxprod-mcp/" + VERSION
    sys_version = ""
    timeout = 120
    # Headers and body leave in two writes.  With Nagle's algorithm a small
    # body waited for the ACK of the headers, and a client that delays its
    # ACKs (lwIP on a microcontroller: up to 250 ms) got every small reply
    # that much later.
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):      # one line per request is printed by _log
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PUT(self):
        self._dispatch("PUT")

    # -- plumbing
    def _send(self, status, body=b"", ctype="application/json; charset=utf-8", extra=None,
              close=False):
        self.send_response(status)
        if body or status not in (202, 204):
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if body:
            self.wfile.write(body)
        self._status, self._nbytes = status, len(body)

    def _json(self, status, obj, extra=None, close=False):
        self._send(status, _dumps(obj).encode("utf-8"), extra=extra, close=close)

    def _log(self, method):
        dt = (time.monotonic() - self._t0) * 1000.0
        line = "%s %s %s %s %s %dB %.1fms" % (_utc_now(), self.client_address[0], method,
                                              self.path[:160], self._status, self._nbytes, dt)
        if self._notes:
            line += " " + " ".join(self._notes)[:200]
        self.server.log_line(line)

    def _read_body(self, cap):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            buf = bytearray()
            while True:
                line = self.rfile.readline(1024)
                try:
                    n = int(line.split(b";")[0].strip(), 16)
                except ValueError:
                    raise _HttpError(400, "bad_chunked_encoding", close=True)
                if n == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(buf)
                if len(buf) + n > cap:
                    raise _HttpError(413, "body_too_large", close=True)
                buf += self.rfile.read(n)
                self.rfile.readline(16)
        cl = self.headers.get("Content-Length")
        if cl is None:
            raise _HttpError(411, "length_required", close=True)
        try:
            n = int(cl)
        except ValueError:
            raise _HttpError(400, "bad_content_length", close=True)
        if n < 0 or n > cap:
            raise _HttpError(413, "body_too_large", close=True)
        body = self.rfile.read(n)
        if len(body) != n:
            raise _HttpError(400, "short_body", close=True)
        return body

    def _dispatch(self, method):
        self._t0 = time.monotonic()
        self._status, self._nbytes, self._notes = None, 0, []
        self._cors = None          # the handler object lives on across keep-alive requests
        try:
            path = urlsplit(self.path).path
            if not host_allowed(self.headers.get("Host"), self.server.extra_hosts):
                self._notes.append("host=%r" % (self.headers.get("Host"),))
                self._json(403, {"error": "forbidden_host",
                                 "message": "Host header not allowed: use an IP address, "
                                            "localhost, a .local name or a configured name"},
                           close=True)
            elif path == "/mcp":
                if method == "POST":
                    self._mcp()
                else:
                    self._json(405, {"error": "method_not_allowed"}, extra={"Allow": "POST"})
            elif path == "/upload":
                if method == "OPTIONS":
                    self._upload_preflight()
                elif method == "POST":
                    self._upload()
                else:
                    self._json(405, {"error": "method_not_allowed"},
                               extra={"Allow": "POST, OPTIONS"})
            elif path == "/healthz" and method == "GET":
                st = self.server.store.stats()
                self._json(200, {"ok": True, "version": VERSION, "apps": st["apps_total"],
                                 "files": st["files_total"], "file_errors": st["file_errors"],
                                 "skill": os.path.isfile(SKILL_FILE),
                                 "mdns": self.server.mdns_info()})
            else:
                self._json(404, {"error": "not_found"})
        except _HttpError as e:
            try:
                self._json(e.status, {"error": e.error}, extra=self._cors, close=e.close)
            except OSError:
                self._status = self._status or e.status
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self._status = self._status or 499
            self.close_connection = True
        except Exception as e:                      # pragma: no cover - logged
            traceback.print_exc()
            try:
                self._json(500, {"error": "internal_error", "message": str(e)[:200]},
                           close=True)
            except OSError:
                pass
        finally:
            self._log(method)

    # -- /mcp
    def _mcp(self):
        origin = self.headers.get("Origin")
        if origin is not None and not mcp_origin_allowed(origin, self.server.extra_hosts):
            raise _HttpError(403, "forbidden_origin")
        body = self._read_body(MAX_MCP_BODY)
        try:
            msg = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            self._json(400, _rpc_error(None, -32700, "Parse error"))
            return
        store = self.server.store
        if isinstance(msg, list):
            if not msg:
                self._json(400, _rpc_error(None, -32600, "Invalid Request"))
                return
            out = [r for r in (handle_rpc(m, store, self._notes) for m in msg) if r is not None]
            if out:
                self._json(200, out)
            else:
                self._send(202)
            return
        resp = handle_rpc(msg, store, self._notes)
        if resp is None:
            self._send(202)
        else:
            self._json(200, resp)

    # -- /upload
    def _cors_headers(self, origin):
        h = {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
             "Access-Control-Allow-Methods": "POST, OPTIONS",
             "Access-Control-Allow-Headers": "Content-Type",
             "Access-Control-Max-Age": "600"}
        if (self.headers.get("Access-Control-Request-Private-Network") or "").lower() == "true":
            h["Access-Control-Allow-Private-Network"] = "true"
        return h

    def _upload_preflight(self):
        origin = self.headers.get("Origin") or ""
        if not upload_origin_allowed(origin, self.server.upload_origins):
            raise _HttpError(403, "origin_not_allowed")
        self._send(204, extra=self._cors_headers(origin))

    def _upload(self):
        origin = self.headers.get("Origin")
        if origin is not None:
            if not upload_origin_allowed(origin, self.server.upload_origins):
                raise _HttpError(403, "origin_not_allowed")
            self._cors = self._cors_headers(origin)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in _UPLOAD_TYPES:
            raise _HttpError(415, "unsupported_content_type", close=True)
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise _HttpError(411, "length_required", close=True)
        cl = self.headers.get("Content-Length")
        if cl is None:
            raise _HttpError(411, "length_required", close=True)
        try:
            length = int(cl)
        except ValueError:
            raise _HttpError(400, "bad_content_length", close=True)
        if length <= 0 or length > MAX_UPLOAD:
            raise _HttpError(413, "upload_too_large", close=True)
        name = _upload_name((parse_qs(urlsplit(self.path).query).get("name") or [""])[0])
        products = self.server.products_dir
        tmp = os.path.join(products, ".upload-%s-%d.tmp" % (
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f"),
            threading.get_ident()))
        try:
            f = open(tmp, "wb")
        except OSError:
            raise _HttpError(503, "product_directory_not_writable", close=True)
        h = hashlib.sha256()
        got, head = 0, b""
        try:
            with f:
                while got < length:
                    chunk = self.rfile.read(min(1 << 20, length - got))
                    if not chunk:
                        break
                    if len(head) < 4:
                        head = (head + chunk)[:4]
                    h.update(chunk)
                    f.write(chunk)
                    got += len(chunk)
                f.flush()
                os.fsync(f.fileno())
            if got != length:
                raise _HttpError(400, "short_body", close=True)
            if head != b"PK\x03\x04":
                raise _HttpError(415, "not_a_zip")
            self._notes.append("upload=%s %dB" % (name, got))
            res, failure = None, None
            try:
                res = store_upload(products, self.server.data_dir, tmp, name, h.hexdigest())
            except (IngestError, zipfile.BadZipFile, ET.ParseError) as e:
                failure = str(e)[:500]
        finally:
            if os.path.exists(tmp):          # gone before any reply leaves
                os.remove(tmp)
        if failure is not None:
            self._json(422, {"ok": False, "error": failure}, extra=self._cors)
            return
        added = set(res["apps_added"])
        entries = [e for e in self.server.store.entries() if e["app_id"] in added]
        reply = {"ok": True, "file": res["file"], "saved_as": res["file"],
                 "stored": res["stored"], "apps_added": res["apps_added"],
                 "apps_new": res.get("apps_new", []), "apps_removed": res.get("apps_removed", []),
                 "products": res.get("products",
                                     len({o for e in entries for o in e.get("order_numbers", [])})),
                 "apps_total": self.server.store.stats()["apps_total"],
                 "comobjects": res.get("comobjects", sum(e.get("comobjects", 0) for e in entries)),
                 "parameters": res.get("parameters", sum(e.get("parameters", 0) for e in entries)),
                 "warnings": res.get("warnings", [])[:20], "seconds": res.get("seconds", 0)}
        for k in ("duplicate_of", "renamed_from"):
            if res.get(k):
                reply[k] = res[k]
        self._notes.append("apps=%d" % len(res["apps_added"]))
        self._json(200, reply, extra=self._cors)


# ---------------------------------------------------------------------------
# server, scanner, service
# ---------------------------------------------------------------------------

def _print_line(line):
    print(line, flush=True)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, family):
        self.address_family = family
        super().__init__(address, handler, bind_and_activate=False)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (OSError, AttributeError):
                pass
        socketserver.TCPServer.server_bind(self)    # no reverse DNS lookup at start
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]


def is_any_address(bind):
    b = (bind or "").strip()
    if b.startswith("[") and b.endswith("]"):
        b = b[1:-1]
    if not b:
        return True
    ip = _ip(b)
    return ip is not None and ip.is_unspecified


MDNS_OFF = {"enabled": False, "name": None, "address": None, "status": "off"}


def make_server(bind, port, data_dir, products_dir, allowed_hosts=(), upload_origins=None,
                allow_any_bind=False, activate=True):
    """Create the HTTP server and bind its socket.  With activate=False it does
    not accept connections yet (Service listens only after the first sync)."""
    b = (bind or "").strip()
    if b.startswith("[") and b.endswith("]"):
        b = b[1:-1]
    if b.lower() == "auto":                 # the LAN address mDNS would announce
        plan = mdns_plan("0.0.0.0")
        if not plan["addresses"]:
            raise ConfigError("--bind auto found no LAN address: %s" % plan["reason"])
        b = plan["interface"]
    if is_any_address(b) and not allow_any_bind:
        raise ConfigError("refusing to listen on all interfaces (%r) without --allow-any-bind "
                          "(KNXPROD_ALLOW_ANY_BIND=1); give an address of this host instead"
                          % (bind,))
    family = socket.AF_INET6 if ":" in b else socket.AF_INET
    httpd = _Server((b, port), Handler, family)
    try:
        httpd.server_bind()
        if activate:
            httpd.server_activate()
    except OSError as e:
        httpd.server_close()
        raise ConfigError("cannot listen on %s port %d: %s" % (b or "*", port, e.strerror or e))
    httpd.store = Store(data_dir)
    httpd.data_dir = os.path.abspath(data_dir)
    httpd.products_dir = os.path.abspath(products_dir)
    httpd.extra_hosts = frozenset(normalize_host_entry(h) for h in allowed_hosts)
    httpd.upload_origins = (upload_origins if isinstance(upload_origins, frozenset)
                            else parse_upload_origins(upload_origins))
    httpd.log_line = _print_line
    httpd.mdns_info = lambda: dict(MDNS_OFF)
    return httpd


class ProductScanner(threading.Thread):
    """Re-syncs the product directory every `interval` seconds."""

    def __init__(self, products_dir, data_dir, interval, log):
        super().__init__(name="knxprod-scan", daemon=True)
        self.products_dir, self.data_dir = products_dir, data_dir
        self.interval, self.log = interval, log
        self.stop_event = threading.Event()

    def run(self):
        missing = False
        while not self.stop_event.wait(self.interval):
            try:
                s = sync_products(self.products_dir, self.data_dir, log=self.log)
                if s["missing"] != missing:
                    missing = s["missing"]
                    self.log("%s product directory %s %s" % (
                        _utc_now(), self.products_dir,
                        "is missing; the index stays as it is" if missing else "is back"))
            except Exception:                         # pragma: no cover - logged
                self.log("%s scan failed: %s" % (_utc_now(), traceback.format_exc(limit=3)))


class Service:
    """Prepare directories, listen, sync the product directory, then serve and
    re-scan.  serve() uses it; tests drive it step by step."""

    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or _print_line
        self.products = os.path.abspath(cfg["products"])
        self.data = os.path.abspath(cfg["data"])
        _prepare_dirs(self.products, self.data)
        self.httpd = make_server(cfg["bind"], cfg["port"], self.data, self.products,
                                 cfg.get("allowed_hosts", ()), cfg.get("upload_origins"),
                                 cfg.get("allow_any_bind", False), activate=False)
        self.httpd.log_line = self.log
        self.port = self.httpd.server_address[1]
        self.scanner = None
        self.mdns = None
        if read_skill() is None:
            self.log("%s warning: %s is missing, so the skill resource %s is not served"
                     % (_utc_now(), SKILL_FILE, SKILL_URI))

    def initial_sync(self):
        interval = self.cfg.get("scan_interval", DEFAULT_SCAN_INTERVAL)
        s = sync_products(self.products, self.data,
                          settle=SETTLE_SECONDS if interval > 0 else 0, log=self.log)
        self.log("%s product directory %s: %d file(s), %d application program(s), "
                 "%d error(s), %d pending, %.1f s" % (
                     _utc_now(), self.products, s["files_total"], s["apps_total"],
                     s["file_errors"], len(s["pending"]), s["seconds"]))
        return s

    def activate(self):
        self.httpd.server_activate()

    def start_scanner(self):
        interval = self.cfg.get("scan_interval", DEFAULT_SCAN_INTERVAL)
        if interval > 0:
            self.scanner = ProductScanner(self.products, self.data, interval, self.log)
            self.scanner.start()

    def start_mdns(self):
        """Announce the service via mDNS unless switched off or pointless."""
        m = self.cfg.get("mdns") or {}
        if not m.get("enabled"):
            return None
        plan = mdns_plan(self.httpd.server_address[0], m.get("address") or None)
        if not plan["addresses"]:
            self.log("%s %s" % (_utc_now(), plan["reason"]))
            info = dict(MDNS_OFF, status="skipped", reason=plan["reason"])
            self.httpd.mdns_info = lambda: dict(info)
            return None
        hostname = m.get("hostname") or default_mdns_hostname()
        core = MdnsCore(m.get("name") or "knxprod-mcp on %s" % hostname, hostname, self.port,
                        plan["addresses"], ["txtvers=1", "path=/mcp", "v=%s" % VERSION])
        self.mdns = MdnsResponder(core, plan, self.log)
        self.httpd.mdns_info = self.mdns.info
        self.mdns.start()
        return self.mdns

    def close(self):
        if self.mdns:
            self.mdns.stop()                  # goodbye (TTL 0) first
        if self.scanner:
            self.scanner.stop_event.set()
        self.httpd.server_close()
        self.httpd.store.close()


def serve(cfg):
    svc = Service(cfg)
    svc.initial_sync()
    svc.activate()
    info_path = os.path.join(svc.data, "server.json")
    atomic_write_json(info_path, {"host": svc.httpd.server_address[0], "port": svc.port,
                                  "pid": os.getpid(), "version": VERSION, "started": _utc_now()})
    svc.start_scanner()
    svc.start_mdns()

    def _stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    host = svc.httpd.server_address[0]
    svc.log("%s knxprod-mcp %s listening on http://%s:%d/mcp%s" % (
        _utc_now(), VERSION, "[%s]" % host if ":" in host else host, svc.port,
        "  (all interfaces)" if is_any_address(host) else ""))
    try:
        svc.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.close()
        try:
            os.remove(info_path)
        except OSError:
            pass
        svc.log("%s knxprod-mcp stopped" % _utc_now())
    return 0


def health_check(data_dir, timeout=4.0):
    """Exit status for container health checks: 0 if the server that wrote
    DATA/server.json answers /healthz with ok."""
    info = _load_json(os.path.join(os.path.abspath(data_dir), "server.json"))
    if not info:
        print("knxprod-mcp: no running server found (server.json missing)", file=sys.stderr)
        return 1
    host, port = info.get("host") or "", int(info.get("port") or 0)
    ip = _ip(host) if host else None
    if not host or (ip is not None and ip.is_unspecified):
        host = "::1" if ip is not None and ip.version == 6 else "127.0.0.1"
    url = "http://%s:%d/healthz" % ("[%s]" % host if ":" in host else host, port)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            ok = r.status == 200 and json.loads(r.read()).get("ok") is True
    except (OSError, ValueError):
        ok = False
    if not ok:
        print("knxprod-mcp: %s is not healthy" % url, file=sys.stderr)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# configuration and CLI
# ---------------------------------------------------------------------------

def _env(name):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def _env_list(name):
    v = _env(name)
    return [x.strip() for x in v.split(",") if x.strip()] if v else []


def build_parser():
    ap = argparse.ArgumentParser(
        prog="knxprod_mcp.py",
        description="MCP server for KNX manufacturer product files (.knxprod). "
                    "Every option falls back to an environment variable.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("serve", help="sync the product directory, then serve MCP over HTTP")
    py = sub.add_parser("sync", help="sync the product directory with the index once and exit")
    ph = sub.add_parser("health", help="exit 0 if the running server answers /healthz")
    ph.add_argument("--data", help="index directory (KNXPROD_DATA, default %s)" % DEFAULT_DATA)
    for p in (ps, py):
        p.add_argument("--products", help="product directory, scanned recursively for "
                                          ".knxprod/.zip (KNXPROD_DIR, default %s)"
                                          % DEFAULT_PRODUCTS)
        p.add_argument("--data", help="index directory (KNXPROD_DATA, default %s)" % DEFAULT_DATA)
        p.add_argument("--zip-max-mb", type=int,
                       help="largest uncompressed product data one file may hold, in MB "
                            "(KNXPROD_ZIP_MAX_MB, default %d)" % (ZIP_MAX_TOTAL >> 20))
    ps.add_argument("--bind", help="listen address, or 'auto' for this host's LAN address "
                                   "(KNXPROD_BIND, default %s)" % DEFAULT_BIND)
    ps.add_argument("--port", type=int, help="port (KNXPROD_PORT, default %d)" % DEFAULT_PORT)
    ps.add_argument("--allowed-host", action="append",
                    help="extra host name clients may use, repeatable "
                         "(KNXPROD_ALLOWED_HOSTS, comma separated)")
    ps.add_argument("--upload-origin", action="append",
                    help="browser origin allowed to upload, repeatable; replaces the default "
                         "private-network rule; 'none' disables browser uploads "
                         "(KNXPROD_UPLOAD_ORIGINS, comma separated)")
    ps.add_argument("--scan-interval", type=float,
                    help="seconds between scans of the product directory, 0 = only at start "
                         "(KNXPROD_SCAN_INTERVAL, default %g)" % DEFAULT_SCAN_INTERVAL)
    ps.add_argument("--allow-any-bind", action="store_true",
                    help="allow listening on 0.0.0.0 / :: (KNXPROD_ALLOW_ANY_BIND=1)")
    ps.add_argument("--no-mdns", action="store_true",
                    help="do not announce the service via mDNS/DNS-SD (KNXPROD_MDNS=0)")
    ps.add_argument("--mdns-name", help="DNS-SD instance name (KNXPROD_MDNS_NAME, "
                                        "default 'knxprod-mcp on <hostname>')")
    ps.add_argument("--mdns-hostname", help="host name announced as <name>.local "
                                            "(KNXPROD_MDNS_HOSTNAME, default this host's name)")
    ps.add_argument("--mdns-address", action="append",
                    help="IPv4 address to announce, repeatable (KNXPROD_MDNS_ADDRESS, comma "
                         "separated; default: the listen address or the LAN address)")
    return ap


def resolve_config(args):
    """Command-line values win over environment variables, which win over defaults."""
    cfg = {"cmd": args.cmd,
           "products": getattr(args, "products", None) or _env("KNXPROD_DIR") or DEFAULT_PRODUCTS,
           "data": args.data or _env("KNXPROD_DATA") or DEFAULT_DATA}
    zmax = getattr(args, "zip_max_mb", None)
    if zmax is None:
        zmax = _env("KNXPROD_ZIP_MAX_MB") or (ZIP_MAX_TOTAL >> 20)
    try:
        cfg["zip_max_mb"] = int(zmax)
    except ValueError:
        raise ConfigError("zip-max-mb must be a number of megabytes: %r" % zmax)
    if cfg["zip_max_mb"] < 1:
        raise ConfigError("zip-max-mb must be at least 1")
    if args.cmd != "serve":
        return cfg
    cfg["bind"] = args.bind or _env("KNXPROD_BIND") or DEFAULT_BIND
    port = args.port if args.port is not None else _env("KNXPROD_PORT") or DEFAULT_PORT
    try:
        port = int(port)
    except ValueError:
        raise ConfigError("port must be a number: %r" % port)
    if not 0 <= port <= 65535:
        raise ConfigError("port out of range: %d" % port)
    cfg["port"] = port
    hosts = args.allowed_host if args.allowed_host else _env_list("KNXPROD_ALLOWED_HOSTS")
    cfg["allowed_hosts"] = [normalize_host_entry(h) for h in hosts]
    origins = args.upload_origin if args.upload_origin else (
        _env_list("KNXPROD_UPLOAD_ORIGINS") or None)
    cfg["upload_origins"] = parse_upload_origins(origins)
    interval = args.scan_interval if args.scan_interval is not None else (
        _env("KNXPROD_SCAN_INTERVAL") or DEFAULT_SCAN_INTERVAL)
    try:
        interval = float(interval)
    except ValueError:
        raise ConfigError("scan interval must be a number of seconds: %r" % interval)
    cfg["scan_interval"] = interval
    cfg["allow_any_bind"] = bool(args.allow_any_bind) or (
        (_env("KNXPROD_ALLOW_ANY_BIND") or "").lower() in ("1", "true", "yes", "on"))
    name = args.mdns_name or _env("KNXPROD_MDNS_NAME")
    if name is not None and not 0 < len(name.strip().encode("utf-8")) <= 63:
        raise ConfigError("mDNS instance name must have 1..63 bytes: %r" % name)
    hostname = args.mdns_hostname or _env("KNXPROD_MDNS_HOSTNAME")
    if hostname is not None:
        h = hostname.strip().rstrip(".")
        if h.lower().endswith(".local"):
            h = h[:-6]
        if not re.match(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$", h):
            raise ConfigError("mDNS host name must be one DNS label: %r" % hostname)
        hostname = h
    raw = args.mdns_address if args.mdns_address else _env_list("KNXPROD_MDNS_ADDRESS")
    addresses = []
    for a in (x.strip() for v in raw for x in v.split(",")):
        if not a:
            continue
        try:
            addresses.append(str(ipaddress.IPv4Address(a)))
        except ValueError:
            raise ConfigError("mDNS address must be an IPv4 address: %r" % a)
    cfg["mdns"] = {"enabled": not args.no_mdns and (_env("KNXPROD_MDNS") or "1").lower()
                   not in ("0", "false", "no", "off"),
                   "name": name.strip() if name else None, "hostname": hostname,
                   "address": addresses or None}
    return cfg


def main(argv=None):
    global ZIP_MAX_TOTAL
    args = build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
        ZIP_MAX_TOTAL = cfg["zip_max_mb"] << 20
        if cfg["cmd"] == "serve":
            return serve(cfg)
        if cfg["cmd"] == "health":
            return health_check(cfg["data"])
        _prepare_dirs(os.path.abspath(cfg["products"]), os.path.abspath(cfg["data"]))
        s = sync_products(cfg["products"], cfg["data"], settle=0, log=_print_line)
    except ConfigError as e:
        print("knxprod-mcp: %s" % e, file=sys.stderr)
        return 2
    print("product files: %d (added %d, changed %d, removed %d, unchanged %d, errors %d); "
          "application programs in the index: %d; %.1f s" % (
              s["files_total"], len(s["added"]), len(s["changed"]), len(s["removed"]),
              s["unchanged"], len(s["errors"]), s["apps_total"], s["seconds"]))
    for rel, err in sorted(s["errors"].items()):
        print("  error: %s: %s" % (rel, err))
    return 1 if s["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
