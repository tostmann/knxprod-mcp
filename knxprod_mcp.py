#!/usr/bin/env python3
"""knxprod-mcp -- MCP server for KNX manufacturer product files (.knxprod).

Point it at a directory of .knxprod files (or zip archives that contain
them).  It distills every application program into a small index and serves
read-only MCP tools about com objects, parameters and memory layout over
Streamable HTTP.  Python standard library only.

    knxprod_mcp.py serve [--products DIR] [--data DIR] [--bind ADDR] [--port N] ...
    knxprod_mcp.py sync  [--products DIR] [--data DIR]

Every option has an environment fallback: KNXPROD_DIR, KNXPROD_DATA,
KNXPROD_BIND, KNXPROD_PORT, KNXPROD_ALLOWED_HOSTS, KNXPROD_UPLOAD_ORIGINS,
KNXPROD_SCAN_INTERVAL, KNXPROD_ALLOW_ANY_BIND.

The product directory is the source of truth.  On start the server syncs
the index with it (new or changed files are ingested, the application
programs of removed files are dropped), then serves, and re-scans it every
KNXPROD_SCAN_INTERVAL seconds.  It never moves, renames or modifies a file
there; uploads are added as new files.  The index lives in the data
directory: DATA/apps/<APPID>.json, DATA/index.json, DATA/files.json (sha256
per product file) and DATA/master.json.  APPID is manufacturer (4) +
application number (4) + version (2) as upper-case hex, e.g. FFF0123456:
the ApplicationId a device reports about itself.

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
"""

import argparse
import base64
import collections
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

VERSION = "0.2.0"
SCHEMA_VERSION = 2
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
MAX_UPLOAD = 64 << 20
ZIP_MAX_TOTAL = 512 << 20          # declared uncompressed bytes, all nesting levels
ZIP_MAX_MEMBERS = 5000             # members, all nesting levels
ZIP_MAX_DEPTH = 3
CACHE_BYTES = 24 << 20             # app JSON kept in memory, counted by file size
MAX_CONDS = 3                      # innermost ETS conditions kept per object/parameter

SLOT_NOTE = ("Names label slots of the application program, not what an "
             "installation uses them for; the channel mode decides.")
VARIANT_NOTE = ("Pick the variant whose size_bits matches the object size the "
                "device reports; 'when' is the ETS condition for a variant.")

INSTRUCTIONS = (
    "knxprod-mcp is a read-only service for a private network. It answers "
    "questions about KNX application programs from the manufacturer product "
    "files (.knxprod) its operator put into its product directory. The data is "
    "derived from manufacturer data licensed to that operator: use it to "
    "understand and configure your own devices, do not republish it.\n"
    "Identify a device by the ApplicationId it reports: 5 bytes = manufacturer "
    "(2), application number (2), version (1), written as 10 hex digits, e.g. "
    "FFF0123456. prod-applications lists what is indexed.\n"
    "Names name SLOTS, not functions: a com-object or parameter name says which "
    "slot of the application program it is. What the device really does there "
    "depends on the channel mode and on the project (in switch mode a 'slat "
    "position' object may switch a pump). Do not present a name as the function "
    "of an installation without checking the configuration.\n"
    "One com-object number can have several variants (ComObjectRef) that differ "
    "in size, flags or text. Pick the variant whose size_bits matches the object "
    "size the device itself reports; 'when' shows the ETS condition (channel "
    "mode) under which a variant is shown.\n"
    "Parameters: address = segment base + offset; bit_offset counts from the MSB "
    "of the first byte; multi-byte values follow the byte order the program "
    "declares (byte_order) -- heed a byte_order_warning. Union members share "
    "memory: only the member whose 'when' matches the configured mode is valid. "
    "Nothing here writes to a device."
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
    """Everything of one <ApplicationProgram>, still with full XML ids."""

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
        self.dynamic = {"co": {}, "pr": {}}
        self.skipped_module_records = 0

    # -- Static/Code
    def add_abs_segment(self, el):
        sid = el.get("Id")
        size = _int(el.get("Size"))
        self.segments[sid] = {"kind": "absolute", "address": _int(el.get("Address")),
                              "size": size}
        for c in el:
            if _local(c.tag) == "Data" and c.text:
                try:
                    data = base64.b64decode(c.text)
                except ValueError:
                    data = None
                if data is not None:
                    self.seg_data[sid] = data[:size] if size else data

    def add_rel_segment(self, el):
        self.segments[el.get("Id")] = {"kind": "relative", "offset": _int(el.get("Offset")),
                                       "size": _int(el.get("Size")),
                                       "lsm": _int(el.get("LoadStateMachine"))}

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
                        vals.append({"id": e.get("Id"), "value": v if v is not None else e.get("BinaryValue"),
                                     "text": e.get("Text")})
                t = {"kind": "enum", "size_bits": _int(c.get("SizeInBit")), "base": c.get("Base"),
                     "values": vals}
            elif k == "TypeText":
                t = {"kind": "text", "size_bits": _int(c.get("SizeInBit"))}
            elif k == "TypeFloat":
                t = {"kind": "float", "size_bits": _int(c.get("SizeInBit")),
                     "encoding": c.get("Encoding"),
                     "min": _num(c.get("minInclusive")), "max": _num(c.get("maxInclusive"))}
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
        shifted by a union member's own Offset/BitOffset."""
        total = ((_int(m.get("Offset"), 0) + add_offset) * 8
                 + _int(m.get("BitOffset"), 0) + add_bits)
        if _local(m.tag) == "Memory":
            return {"kind": "memory", "segment": m.get("CodeSegment"),
                    "offset": total // 8, "bit_offset": total % 8}
        loc = {"kind": "property", "offset": total // 8, "bit_offset": total % 8}
        for key, attr in (("object_index", "ObjectIndex"), ("object_type", "ObjectType"),
                          ("pid", "PropertyId"), ("occurrence", "Occurrence")):
            v = _int(m.get(attr))
            if v is not None:
                loc[key] = v
        return loc

    @staticmethod
    def _param_base(el):
        return {"id": el.get("Id"), "name": el.get("Name"), "text": el.get("Text"),
                "type": el.get("ParameterType"), "access": el.get("Access"),
                "default": el.get("Value"), "loc": None}

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
                                "access": el.get("Access")})

    # -- Static/ComObjectTable, ComObjectRefs
    def add_comobject(self, el):
        self.comobjects.append({
            "id": el.get("Id"), "number": _int(el.get("Number")), "name": el.get("Name"),
            "text": el.get("Text"), "function_text": el.get("FunctionText"),
            "size": el.get("ObjectSize"), "priority": el.get("Priority"),
            "flags": {k: el.get(a) for k, a in _FLAG_ATTRS}, "dpt": el.get("DatapointType")})

    def add_comobject_ref(self, el):
        self.comobject_refs.append({
            "id": el.get("Id"), "ref": el.get("RefId"), "text": el.get("Text"),
            "function_text": el.get("FunctionText"), "size": el.get("ObjectSize"),
            "priority": el.get("Priority"), "flags": {k: el.get(a) for k, a in _FLAG_ATTRS},
            "dpt": el.get("DatapointType")})

    # -- Static/LoadProcedures
    def add_load_procedure(self, el):
        steps = [(_local(c.tag), dict(c.attrib)) for c in el if _local(c.tag).startswith("LdCtrl")]
        self.ldprocs.append((el.get("MergeId"), steps))


def _note_ref(table, refid, conds, block):
    if not refid:
        return
    r = table.get(refid)
    if r is None:
        table[refid] = [1, conds[-MAX_CONDS:], block]
    else:
        r[0] += 1


def walk_dynamic(dyn):
    """Record, per ComObjectRef/ParameterRef id, how often the ETS UI shows it
    and the innermost conditions (choose/when) plus block of its first
    occurrence.  Iterative: the nesting can be deep."""
    co, pr = {}, {}
    stack = [[iter(list(dyn)), (), None, None]]
    while stack:
        frame = stack[-1]
        c = next(frame[0], None)
        if c is None:
            stack.pop()
            continue
        conds, block, choose = frame[1], frame[2], frame[3]
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
            stack.append([iter(list(c)), conds + ((cond,) if cond else ()), block, None])
        elif k == "choose":
            whens = [w for w in c if _local(w.tag) == "when"]
            tests = tuple(w.get("test") for w in whens
                          if w.get("test") is not None
                          and (w.get("default") or "").lower() not in ("true", "1"))
            stack.append([iter(whens), conds, block, (c.get("ParamRefId"), tests)])
        elif k == "ComObjectRefRef":
            _note_ref(co, c.get("RefId"), conds, block)
        elif k == "ParameterRefRef":
            _note_ref(pr, c.get("RefId"), conds, block)
        elif k in ("ParameterBlock", "Channel", "ChannelIndependentBlock"):
            nb = (k, c.get("Id"), c.get("Text"), c.get("ParamRefId"), c.get("Name"))
            stack.append([iter(list(c)), conds, nb, None])
        else:
            stack.append([iter(list(c)), conds, block, None])
    return {"co": co, "pr": pr}


# elements whose children are complete records that were already processed
_APP_CONTAINERS = {"Code", "ParameterTypes", "Parameters", "ParameterRefs", "ComObjectTable",
                   "ComObjectRefs", "LoadProcedures", "Static", "ModuleDefs"}


def parse_application_xml(stream):
    """Stream-parse one application program XML.  Returns ([_RawApp], langs).

    A record (Parameter, Union, ComObject, ...) is processed at ITS end event,
    when all of its children are complete, and cleared only afterwards.  The
    <Parameter> children of a <Union> are deliberately NOT handled at their
    own end events: clearing them there would wipe Offset/BitOffset before
    the union is processed (trap 4)."""
    apps = []
    langs = {}
    cur = None
    lang = None
    tags = []
    in_modules = 0
    for event, el in ET.iterparse(stream, events=("start", "end")):
        tag = _local(el.tag)
        if event == "start":
            tags.append(tag)
            if tag == "ApplicationProgram":
                cur = _RawApp(dict(el.attrib))
            elif tag == "ModuleDefs":
                in_modules += 1
            elif cur is not None and not in_modules and tag in (
                    "ComObjectTable", "AddressTable", "AssociationTable"):
                cur.tables[tag] = dict(el.attrib)
            elif cur is not None and not in_modules and tag == "Options":
                cur.options = dict(el.attrib)
            elif tag == "Language":
                lang = el.get("Identifier") or ""
            continue
        tags.pop()
        parent = tags[-1] if tags else ""
        if tag == "TranslationElement":
            _collect_translation(langs, lang, el)
            el.clear()
        elif cur is not None:
            if in_modules:
                # Parameters inside module definitions are placed through
                # arguments; not resolved here (only counted).
                if tag == "ModuleDefs":
                    in_modules -= 1
                    el.clear()
                elif tag in ("Parameter", "Union", "ComObject") and parent in (
                        "Parameters", "ComObjectTable"):
                    cur.skipped_module_records += 1
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
                cur.add_parameter(el)
                el.clear()
            elif tag == "Union" and parent == "Parameters":
                cur.add_union(el)
                el.clear()
            elif tag == "ParameterRef" and parent == "ParameterRefs":
                cur.add_parameter_ref(el)
                el.clear()
            elif tag == "ComObject" and parent == "ComObjectTable":
                cur.add_comobject(el)
                el.clear()
            elif tag == "ComObjectRef" and parent == "ComObjectRefs":
                cur.add_comobject_ref(el)
                el.clear()
            elif tag == "LoadProcedure" and parent == "LoadProcedures":
                cur.add_load_procedure(el)
                el.clear()
            elif tag == "Dynamic" and parent == "ApplicationProgram":
                cur.dynamic = walk_dynamic(el)
                el.clear()
            elif tag in _APP_CONTAINERS:
                el.clear()
            elif tag == "ApplicationProgram":
                apps.append(cur)
                cur = None
                el.clear()
        elif tag in ("TranslationUnit", "Language", "Languages", "ApplicationPrograms"):
            el.clear()
    return apps, langs


def parse_hardware_xml(stream):
    """Hardware.xml -> products and Hardware2Program -> application links."""
    programs = {}
    products = {}
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
    return {"programs": programs, "products": products, "langs": langs}


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


def build_record(raw, langs, master, hw, source):
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

    # -- ETS conditions (Dynamic) rendered as text
    params_by_id = {p["id"]: p for p in raw.params}
    pref_by_id = {r["id"]: r for r in raw.param_refs}

    def pref_text(pref_id):
        r = pref_by_id.get(pref_id)
        if r is None:
            return short(pref_id) or "?", None
        p = params_by_id.get(r["ref"])
        text = None
        if r.get("text"):
            text = T.get(pref_id, "Text", r["text"])[0]
        elif p is not None:
            text = T.get(p["id"], "Text", p["text"])[0]
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

    def render_cond(cond):
        pref, test, others = cond
        text, p = pref_text(pref)
        if test is not None:
            vals = test.split()
            if vals and all(_is_intlike(v) for v in vals):
                shown = []
                for v in vals[:6]:
                    vt = value_text(p, v)
                    shown.append("%s (%s)" % (v, vt) if vt else v)
                if len(vals) == 1:
                    return "%s = %s" % (text, shown[0])
                return "%s in {%s}" % (text, ", ".join(shown))
            return "%s %s" % (text, test)
        return "%s = other (not %s)" % (text, " | ".join(others))

    def block_label(b):
        if b is None:
            return None
        _kind, bid, text, pref_id, nm = b
        if text:
            return T.get(bid, "Text", text)[0] or None
        if pref_id:
            return pref_text(pref_id)[0] or None
        return _norm(nm) or None

    def context(entry):
        if entry is None:
            return {"ui": False}
        count, conds, b = entry
        o = {}
        w = [render_cond(c) for c in conds]
        if w:
            o["when"] = w
        bl = block_label(b)
        if bl:
            o["block"] = bl
        if count > 1:
            o["seen"] = count
        return o

    # -- parameters
    refs_by_param = collections.defaultdict(list)
    for r in raw.param_refs:
        refs_by_param[r["ref"]].append(r)
    params_out = []
    counts = collections.Counter()
    tcheck = [0, 0]
    declared_order = raw.options.get("ParameterByteOrder")
    order = byte_order_of(declared_order)
    # discriminating evidence from the segment template (see below)
    evidence = collections.Counter()
    for seq, p in enumerate(raw.params):
        pid = p["id"]
        text, text_de = T.get(pid, "Text", p["text"])
        o = {"id": short(pid), "text": text}
        if text_de:
            o["text_de"] = text_de
        nm = _norm(p.get("name"))
        if nm and nm != text and nm != text_de:
            o["name"] = nm
        access = p.get("access") or "ReadWrite"
        if access != "ReadWrite":
            o["access"] = access
        tshort = short(p.get("type")) if p.get("type") else None
        ty = types_out.get(tshort) if tshort else None
        if tshort:
            o["type"] = tshort
        size = ty.get("size_bits") if ty else None
        if size is not None:
            o["size_bits"] = size
        if p.get("default") is not None:
            o["default"] = p["default"]
        loc = p.get("loc")
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
        variants = []
        entry, seen = None, 0
        for r in refs_by_param.get(pid, []):
            v = {}
            if r.get("text"):
                t2 = T.get(r["id"], "Text", r["text"])[0]
                if t2 and t2 != text:
                    v["text"] = t2
            if r.get("default") is not None and r["default"] != p.get("default"):
                v["default"] = r["default"]
            if r.get("access") and r["access"] != access:
                v["access"] = r["access"]
            if v:
                v["ref"] = short_ref(r["id"], pid)
                variants.append(v)
            e = raw.dynamic["pr"].get(r["id"])
            if e:
                seen += e[0]
                if entry is None:
                    entry = e
        if variants:
            o["variants"] = variants[:8]
            if len(variants) > 8:
                o["variants_total"] = len(variants)
        ctx = context(entry)
        if seen > 1:
            ctx["seen"] = seen
        o.update(ctx)
        # Self-check of the layout conventions against the manufacturer's
        # segment template, which usually (not always) carries the defaults.
        # Only cases where the alternatives give DIFFERENT values count as
        # evidence: MSB- vs LSB-first bit offsets, big- vs little-endian.
        if (loc and loc["kind"] == "memory" and ty and ty.get("kind") in ("number", "enum")
                and size and size <= 64 and (p.get("union") is None or p["union"]["default"])):
            dv = _intval(p.get("default"))
            data = raw.seg_data.get(loc["segment"])
            if dv is not None and data is not None:
                dv &= (1 << size) - 1
                off, bit = loc["offset"], loc["bit_offset"]
                tv = extract_bits(data, off, bit, size, order)
                if tv is not None:
                    tcheck[0] += 1
                    tcheck[1] += int(tv == dv)
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
        o["_seq"] = seq
        params_out.append(o)
    params_out.sort(key=lambda o: (0 if o.get("address") is not None else
                                   1 if o.get("storage") else 2,
                                   o.get("address") or 0, o.get("bit_offset") or 0, o["_seq"]))
    for o in params_out:
        del o["_seq"]

    # -- com objects with all ComObjectRef variants, fully resolved
    corefs = collections.defaultdict(list)
    for r in raw.comobject_refs:
        corefs[r["ref"]].append(r)
    cos_out = []
    for c in raw.comobjects:
        cid = c["id"]
        text, text_de = T.get(cid, "Text", c["text"])
        ft, ft_de = T.get(cid, "FunctionText", c["function_text"])
        o = {"number": c["number"], "id": short(cid), "text": text}
        if text_de:
            o["text_de"] = text_de
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
        for r in corefs.get(cid, []):
            v = {"ref": short_ref(r["id"], cid)}
            if r.get("text"):
                v["text"], vt_de = T.get(r["id"], "Text", r["text"])
                if vt_de:
                    v["text_de"] = vt_de
            else:
                v["text"] = text
            if r.get("function_text"):
                v["function_text"] = T.get(r["id"], "FunctionText", r["function_text"])[0]
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
            v.update(context(raw.dynamic["co"].get(r["id"])))
            variants.append(v)
        o["variants"] = variants
        cos_out.append(o)
    cos_out.sort(key=lambda o: (o["number"] if o["number"] is not None else 1 << 30, o["id"]))

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

    counts = {"comobjects": len(raw.comobjects), "comobject_refs": len(raw.comobject_refs),
              "parameters": len(raw.params), "parameters_memory": counts["parameters_memory"],
              "parameters_union": counts["parameters_union"],
              "parameters_property": counts["parameters_property"],
              "parameters_virtual": counts["parameters_virtual"],
              "parameter_refs": len(raw.param_refs),
              "template_check": {"checked": tcheck[0], "match": tcheck[1]}}
    if raw.skipped_module_records:
        counts["module_records_skipped"] = raw.skipped_module_records
    byte_order = {"declared": declared_order or "not stated (BigEndian assumed)",
                  "used": order,
                  "template_evidence": {"big": evidence["big"], "little": evidence["little"]},
                  "bit_order_evidence": {"msb_first": evidence["msb_first"],
                                         "lsb_first": evidence["lsb_first"]}}
    other = "little" if order == "big" else "big"
    if evidence[other] > evidence[order]:
        byte_order["warning"] = (
            "multi-byte defaults in this program's segment template read as %s-endian, "
            "although it declares %s; check multi-byte values on a device before "
            "writing them" % (other, declared_order or "nothing"))

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
        "counts": counts, "types": types_out, "comobjects": cos_out, "parameters": params_out,
        "source": source,
    }
    if name_de:
        rec["name_de"] = name_de
    if orig:
        rec["original_manufacturer"] = orig
    return rec


# ---------------------------------------------------------------------------
# input enumeration: directory, zip, nested zip (.knxprod in .zip)
# ---------------------------------------------------------------------------

class IngestError(Exception):
    pass


_APP_FILE_RE = re.compile(r"^m-[0-9a-f]{4}_a-[0-9a-f]{4}-[0-9a-f]{2}-.*\.xml$")


def classify_member(name):
    base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base == "knx_master.xml":
        return "master"
    if base == "hardware.xml":
        return "hardware"
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

    def __init__(self, path, tmp_dir, max_total=ZIP_MAX_TOTAL, max_members=ZIP_MAX_MEMBERS,
                 max_depth=ZIP_MAX_DEPTH, label=None):
        self.path = path
        self.label = label
        self.tmp_dir = tmp_dir
        self.max_total, self.max_members, self.max_depth = max_total, max_members, max_depth
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
        elif os.path.isfile(path) and classify_member(path) in ("app", "master", "hardware"):
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
        self._total += sum(i.file_size for i in infos)
        if self._total > self.max_total:
            raise IngestError("uncompressed size exceeds %d MB" % (self.max_total >> 20))
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
# index maintenance: product files -> application records
#
# Every application record knows the product files ("sources") it was found
# in.  A file that disappears or changes takes its applications with it unless
# another file still provides them.  All writes go through _IngestLock and are
# atomic, so a reader always sees a complete index.
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
         "comobjects": rec["counts"]["comobjects"], "parameters": rec["counts"]["parameters"]}
    om = rec.get("original_manufacturer")
    if om:
        e["original_manufacturer"] = om.get("name")
        e["original_manufacturer_id"] = om.get("id")
    return e


def _build_index(apps):
    aliases = {}
    for app_id, e in sorted(apps.items()):
        for al in e.get("aliases", []):
            if al not in apps:
                aliases.setdefault(al, app_id)
    return {"schema": SCHEMA_VERSION, "generated": _utc_now(), "apps": apps, "aliases": aliases}


def _empty_index():
    return {"schema": SCHEMA_VERSION, "apps": {}, "aliases": {}}


def _empty_files():
    return {"schema": SCHEMA_VERSION, "files": {}}


def _paths(data_dir):
    return {"apps": os.path.join(data_dir, "apps"), "tmp": os.path.join(data_dir, "tmp"),
            "index": os.path.join(data_dir, "index.json"),
            "files": os.path.join(data_dir, "files.json"),
            "master": os.path.join(data_dir, "master.json")}


def _drop_source(apps, source, app_ids):
    """Remove `source` from these index entries.  Returns the applications
    left without any source (to delete) and the other sources whose files
    must be read again because the dropped file had written the record."""
    removed, stale = [], set()
    for a in sorted(app_ids):
        e = apps.get(a)
        if e is None:
            continue
        rest = [s for s in e.get("sources", []) if s != source]
        if not rest:
            del apps[a]
            removed.append(a)
        else:
            e["sources"] = rest
            if e.get("written_by") == source:
                stale.add(rest[0])
    return removed, sorted(stale)


def _write_index(data_dir, apps, removed=()):
    p = _paths(data_dir)
    atomic_write_json(p["index"], _build_index(apps))
    for a in removed:                      # only after the index no longer points there
        try:
            os.remove(os.path.join(p["apps"], a + ".json"))
        except FileNotFoundError:
            pass


def _remove_source_locked(data_dir, source):
    p = _paths(data_dir)
    index = _load_json(p["index"]) or _empty_index()
    apps = index.get("apps", {})
    affected = [a for a, e in apps.items() if source in e.get("sources", [])]
    if not affected:
        return [], []
    removed, stale = _drop_source(apps, source, affected)
    _write_index(data_dir, apps, removed)
    return removed, stale


def _ingest_locked(path, data_dir, source, label=None, log=None):
    """Distill every application program in `path` and record it as coming
    from product file `source`.  Applications `source` provided before but no
    longer contains lose it; those left without any source are removed.
    Raises IngestError when nothing usable is inside."""
    t0 = time.monotonic()
    p = _paths(data_dir)
    for d in (data_dir, p["apps"], p["tmp"]):
        os.makedirs(d, exist_ok=True)
    index = _load_json(p["index"]) or _empty_index()
    apps = index.get("apps", {})
    before = set(apps)
    master = _load_json(p["master"]) or {"manufacturers": {}, "masks": {}}
    warnings, written, per_app, product_ids = [], {}, [], set()
    with SourceSet(path, p["tmp"], label=label or source) as src:
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
        hw = {"programs": {}, "products": {}, "langs": {}}
        for m in src.members:
            if m.kind == "hardware":
                with m.open() as f:
                    new = parse_hardware_xml(f)
                for k, lst in new["programs"].items():
                    dst = hw["programs"].setdefault(k, [])
                    dst.extend(x for x in lst if x not in dst)
                hw["products"].update(new["products"])
                for lang, d in new["langs"].items():
                    hw["langs"].setdefault(lang, {}).update(d)
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
            for raw in raws:
                try:
                    rec = build_record(raw, langs, master, hw,
                                       {"member": m.name, "sha256": digest,
                                        "ingested": _utc_now()})
                except Exception as e:      # one broken program must not stop the rest
                    warnings.append("skipped %s in %s: %s: %s" % (
                        raw.full_id, m.name, type(e).__name__, e))
                    continue
                app_id = rec["app_id"]
                if app_id in written:
                    warnings.append("%s defined twice (%s, %s); the later one wins"
                                    % (app_id, written[app_id]["id"], rec["id"]))
                old_entry = apps.get(app_id)
                if not rec["products"] and not kinds.get("hardware") and old_entry:
                    old = _load_json(os.path.join(p["apps"], app_id + ".json"))
                    if old and old.get("products"):
                        rec["products"] = old["products"]
                atomic_write_json(os.path.join(p["apps"], app_id + ".json"), rec)
                entry = _index_entry(rec)
                entry["sources"] = sorted(set((old_entry or {}).get("sources", [])) | {source})
                entry["written_by"] = source
                written[app_id] = entry
                product_ids.update(pr["order_number"] for pr in rec["products"])
                c = rec["counts"]
                per_app.append({"app_id": app_id, "name": rec["name"],
                                "products": len(rec["products"]),
                                "comobjects": c["comobjects"],
                                "comobject_refs": c["comobject_refs"],
                                "parameters": c["parameters"],
                                "parameters_memory": c["parameters_memory"],
                                "parameters_union": c["parameters_union"],
                                "template_check": c["template_check"],
                                "byte_order": rec["byte_order"]})
                if rec["byte_order"].get("warning"):
                    warnings.append("%s: %s" % (app_id, rec["byte_order"]["warning"]))
    if not written:
        raise IngestError("no application program could be distilled from %s" % source)
    previous = {a for a, e in apps.items() if source in e.get("sources", [])}
    apps.update(written)
    removed, stale = _drop_source(apps, source, previous - set(written))
    atomic_write_json(p["master"], master)
    _write_index(data_dir, apps, removed)
    ev = collections.Counter()
    for pa in per_app:
        ev.update(pa["byte_order"]["template_evidence"])
        ev.update(pa["byte_order"]["bit_order_evidence"])
    summary = {
        "source": source, "apps_added": sorted(written),
        "apps_new": sorted(a for a in written if a not in before),
        "apps_removed": removed, "stale_sources": stale, "apps_total": len(apps),
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
    with _IngestLock(data_dir):
        return _ingest_locked(path, data_dir, source or os.path.basename(path.rstrip("/")),
                              log=log)


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


def _file_record(sha, st, summary):
    return {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "status": "ok",
            "apps": summary["apps_added"], "ingested": _utc_now(),
            "warnings": summary["warnings"][:20]}


def sync_products(products_dir, data_dir, settle=SETTLE_SECONDS, log=None):
    """Bring the index in line with the product directory: index new and
    changed files, drop the applications of files that are gone.  A file is
    read only once it has not changed for `settle` seconds; one whose size
    and mtime are unchanged is not even hashed.  A missing product directory
    (not mounted yet?) changes nothing; an empty one empties the index."""
    t0 = time.monotonic()
    products_dir = os.path.abspath(products_dir)
    data_dir = os.path.abspath(data_dir)
    summary = {"added": [], "changed": [], "refreshed": [], "removed": [], "unchanged": 0,
               "pending": [], "errors": {}, "apps_removed": [], "missing": False}
    if not os.path.isdir(products_dir):
        summary["missing"] = True
        idx = _load_json(_paths(data_dir)["index"]) or _empty_index()
        files = (_load_json(_paths(data_dir)["files"]) or _empty_files()).get("files", {})
        summary.update(apps_total=len(idx.get("apps", {})), files_total=len(files),
                       seconds=round(time.monotonic() - t0, 2))
        return summary
    with _IngestLock(data_dir):
        p = _paths(data_dir)
        for d in (data_dir, p["apps"], p["tmp"]):
            os.makedirs(d, exist_ok=True)
        state = _load_json(p["files"]) or _empty_files()
        files = state.setdefault("files", {})
        present = product_files(products_dir)
        index = _load_json(p["index"]) or _empty_index()
        known = set(files)
        for e in index.get("apps", {}).values():
            known.update(e.get("sources", []))
        force = {rel for rel, r in files.items() if r.get("reingest")}

        def queue(rels):
            for rel in rels:
                if rel in present and rel not in force:
                    force.add(rel)
                    todo.append(rel)

        # files that are gone
        for rel in sorted(known - set(present)):
            removed, stale = _remove_source_locked(data_dir, rel)
            files.pop(rel, None)
            summary["removed"].append(rel)
            summary["apps_removed"] += removed
            force.update(s for s in stale if s in present)
            if log:
                log("%s removed %s: %d application program(s) dropped"
                    % (_utc_now(), rel, len(removed)))
        # new and changed files
        now = time.time()
        todo = []
        for rel, (full, st) in sorted(present.items()):
            r = files.get(rel)
            if (r and rel not in force and r.get("size") == st.st_size
                    and r.get("mtime_ns") == st.st_mtime_ns):
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
            if r and r.get("sha256") == sha and rel not in force:
                r["size"], r["mtime_ns"] = st.st_size, st.st_mtime_ns    # touched, same bytes
                summary["unchanged"] += 1
                continue
            force.discard(rel)
            try:
                res = _ingest_locked(full, data_dir, rel, log=log)
            except (IngestError, zipfile.BadZipFile, ET.ParseError, OSError) as e:
                removed, stale = _remove_source_locked(data_dir, rel)
                files[rel] = {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                              "status": "error", "error": str(e)[:300], "apps": [],
                              "checked": _utc_now()}
                summary["errors"][rel] = str(e)[:300]
                summary["apps_removed"] += removed
                queue(stale)
                if log:
                    log("%s cannot index %s: %s" % (_utc_now(), rel, str(e)[:300]))
                continue
            kind = "added" if not r else ("refreshed" if r.get("sha256") == sha else "changed")
            summary[kind].append(rel)
            files[rel] = _file_record(sha, st, res)
            summary["apps_removed"] += res["apps_removed"]
            queue(res["stale_sources"])
        for rel in force:                   # stale but not readable this round
            if rel in files:
                files[rel]["reingest"] = True
        atomic_write_json(p["files"], state)
        idx = _load_json(p["index"]) or _empty_index()
        summary["apps_total"] = len(idx.get("apps", {}))
        summary["files_total"] = len(files)
    summary["seconds"] = round(time.monotonic() - t0, 2)
    return summary


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
    with _IngestLock(data_dir):
        p = _paths(data_dir)
        state = _load_json(p["files"]) or _empty_files()
        files = state.setdefault("files", {})
        for rel, r in sorted(files.items()):
            if (r.get("sha256") == sha and r.get("status") == "ok"
                    and os.path.isfile(os.path.join(products_dir, rel))):
                os.remove(tmp_path)
                return {"stored": False, "duplicate_of": rel, "file": rel,
                        "apps_added": r.get("apps", []), "apps_new": [], "apps_removed": [],
                        "warnings": []}
        stem, ext = name[:-len(name.rsplit(".", 1)[-1]) - 1], "." + name.rsplit(".", 1)[-1]
        candidate, n, existing = name, 1, False
        while os.path.exists(os.path.join(products_dir, candidate)):
            if _sha256_file(os.path.join(products_dir, candidate)) == sha:
                existing = True             # same bytes, not indexed yet: use that file
                break
            n += 1
            candidate = "%s-%d%s" % (stem, n, ext)
        res = _ingest_locked(tmp_path, data_dir, candidate, label=candidate)
        final = os.path.join(products_dir, candidate)
        if existing:
            os.remove(tmp_path)
        else:
            os.replace(tmp_path, final)
            _fsync_dir(products_dir)
        files[candidate] = _file_record(sha, os.stat(final), res)
        atomic_write_json(p["files"], state)
        if res["stale_sources"]:            # cannot happen for a new name; keep it consistent
            for rel in res["stale_sources"]:
                if rel in files:
                    files[rel]["reingest"] = True
            atomic_write_json(p["files"], state)
    out = dict(res)
    out.update({"stored": not existing, "file": candidate})
    if candidate != name:
        out["renamed_from"] = name
    return out


def _prepare_dirs(products_dir, data_dir):
    """Create the directories; fail with a clear message if the index cannot
    be written.  A read-only product directory is fine (uploads then fail)."""
    p = _paths(data_dir)
    try:
        for d in (data_dir, p["apps"], p["tmp"]):
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
    for d, prefix in ((products_dir, ".upload-"), (p["tmp"], "nested-")):
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

class Store:
    def __init__(self, data_dir):
        self.data_dir = os.path.abspath(data_dir)
        self._lock = threading.Lock()
        self._index = _empty_index()
        self._sig = None
        self._cache = collections.OrderedDict()   # app_id -> (record, file size)
        self._cache_bytes = 0

    def _index_path(self):
        return os.path.join(self.data_dir, "index.json")

    def index(self):
        try:
            st = os.stat(self._index_path())
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            sig = None
        with self._lock:
            if sig != self._sig:
                idx = _load_json(self._index_path()) if sig else None
                self._index = idx or _empty_index()
                self._sig = sig
                self._cache.clear()
                self._cache_bytes = 0
            return self._index

    def files(self):
        return (_load_json(os.path.join(self.data_dir, "files.json")) or _empty_files()).get(
            "files", {})

    def resolve(self, app_id):
        idx = self.index()
        if app_id in idx.get("apps", {}):
            return app_id
        return idx.get("aliases", {}).get(app_id)

    def app(self, app_id):
        real = self.resolve(app_id)
        if real is None:
            return None
        with self._lock:
            hit = self._cache.get(real)
            if hit is not None:
                self._cache.move_to_end(real)
                return hit[0]
        path = os.path.join(self.data_dir, "apps", real + ".json")
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                rec = json.load(f)
        except FileNotFoundError:
            return None
        with self._lock:
            if real not in self._cache:
                self._cache[real] = (rec, size)
                self._cache_bytes += size
            while self._cache_bytes > CACHE_BYTES and len(self._cache) > 1:
                _, (_, sz) = self._cache.popitem(last=False)
                self._cache_bytes -= sz
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


def need_app(store, args):
    raw = args.get("app_id")
    app_id = normalize_app_id(raw)
    if app_id is None:
        raise ToolError("invalid_argument",
                        "app_id must be 10 hex digits: manufacturer (4), application number (4), "
                        "version (2), e.g. FFF0123456", app_id=raw)
    rec = store.app(app_id)
    if rec is not None:
        return rec
    idx = store.index()
    cands = []
    for k, e in sorted(idx.get("apps", {}).items()):
        ids = [k] + list(e.get("aliases", []))
        if any(i[4:8] == app_id[4:8] and (i[:4] == app_id[:4] or i[8:] == app_id[8:]) for i in ids):
            cands.append({"app_id": k, "name": e.get("name"),
                          "version_hex": "%02X" % e.get("application_version", 0)})
    raise ToolError("unknown_app_id", "application program %s is not ingested" % app_id,
                    app_id=app_id, candidates=cands[:10],
                    hint="prod-applications lists what is ingested")


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


def _page(items, offset, limit, render, header, key, budget):
    """Serialise items[offset:offset+limit]; return fewer when the reply would
    exceed the byte budget.  Detail levels drop optional fields only when even
    a single item does not fit."""
    total = len(items)
    window = items[offset:offset + limit]

    def build(rendered, n):
        o = dict(header)
        o["total"] = total
        o["offset"] = offset
        o["returned"] = n
        o["next_offset"] = offset + n if offset + n < total else None
        o[key] = rendered[:n]
        return _dumps(o)

    if not window:
        return build([], 0)
    for level in (0, 1, 2, 3):
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
    o = {"app_id": e["app_id"], "name": e.get("name"), "manufacturer": e.get("manufacturer"),
         "version": e.get("application_version"),
         "version_hex": "%02X" % (e.get("application_version") or 0), "mask": e.get("mask"),
         "products": len(e.get("order_numbers", []))}
    if level < 2:
        o["order_numbers"] = e.get("order_numbers", [])[:6 if level == 0 else 3]
    if e.get("aliases"):
        o["aliases"] = e["aliases"]
    if e.get("original_manufacturer") and level == 0:
        o["original_manufacturer"] = e["original_manufacturer"]
    return o


def tool_applications(store, args, budget):
    idx = store.index()
    mfr = _arg_str(args, "manufacturer")
    query = _arg_str(args, "query")
    offset = _arg_int(args, "offset", 0, 0, 1 << 30)
    limit = _arg_int(args, "limit", 40, 1, 200)
    items = []
    for app_id in sorted(idx.get("apps", {})):
        e = idx["apps"][app_id]
        if mfr and not _mfr_match(e, mfr):
            continue
        if query and not _words_match(query, [e.get("name"), e.get("id"), app_id]
                                      + e.get("aliases", []) + e.get("order_numbers", [])
                                      + e.get("product_texts", [])):
            continue
        items.append(e)
    header = {"ingested": len(idx.get("apps", {}))}
    if not idx.get("apps"):
        header["note"] = "nothing ingested yet"
    return _page(items, offset, limit, _render_app_entry, header, "applications", budget)


def tool_application(store, args, budget):
    rec = need_app(store, args)
    entry = store.index().get("apps", {}).get(rec["app_id"], {})
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
        lp = rec.get("load_procedure", {})
        steps = lp.get("steps", [])
        o["load_procedure"] = {"source": lp.get("source"),
                               "steps": steps if cap_s is None else steps[:cap_s]}
        if cap_s is not None and len(steps) > cap_s:
            o["load_procedure"]["steps_total"] = len(steps)
        if entry.get("sources"):
            o["product_files"] = entry["sources"][:8 if cap_p is None else 3]
        o["note"] = SLOT_NOTE
        return o

    return _fit_single(build, len(caps), budget)


def _render_comobject(c, level):
    o = {"number": c["number"], "text": c["text"]}
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
    if level >= 3:
        # Last resort for objects with very many variants (multi-function
        # push-buttons have >100): one entry per distinct size/flags/text.
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
        o["variants_total"] = len(c.get("variants", []))
        o["variant_groups"] = list(groups.values())
        o["variants_note"] = ("too many variants for one reply: grouped by size/flags/text, "
                              "ETS conditions omitted")
        return o
    vs = []
    for v in c.get("variants", []):
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
        if v.get("when"):
            w["when"] = v["when"] if level == 0 else v["when"][-1:]
        if level < 2 and v.get("block"):
            w["block"] = v["block"]
        if level == 0 and v.get("seen"):
            w["seen"] = v["seen"]
        if v.get("ui") is False:
            w["ui"] = False
        vs.append(w)
    o["variants"] = vs
    return o


def tool_comobjects(store, args, budget):
    rec = need_app(store, args)
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
    offset = _arg_int(args, "offset", 0, 0, 1 << 30)
    limit = _arg_int(args, "limit", 40, 1, 200)
    wanted = set(numbers) if numbers is not None else None
    items = []
    for c in rec.get("comobjects", []):
        if wanted is not None and c["number"] not in wanted:
            continue
        if query:
            fields = [c.get("text"), c.get("text_de"), c.get("function_text"),
                      c.get("function_text_de"), c.get("name")]
            for v in c.get("variants", []):
                fields += [v.get("text"), v.get("text_de"), v.get("function_text")]
            if not _words_match(query, fields):
                continue
        items.append(c)
    header = {"app_id": rec["app_id"], "name": rec["name"], "note": SLOT_NOTE + " " + VARIANT_NOTE}
    if wanted is not None:
        found = {c["number"] for c in items}
        missing = sorted(n for n in wanted if n not in found)
        if missing:
            header["missing_numbers"] = missing[:64]
    return _page(items, offset, limit, _render_comobject, header, "comobjects", budget)


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
    elif ty.get("kind") == "enum":
        vals = ty.get("values", [])
        cap = (64, 24, 8, 4)[level]
        o["values"] = vals[:cap]
        if len(vals) > cap:
            o["values_total"] = len(vals)
    return o


def _enum_text(ty, value):
    if ty and ty.get("kind") == "enum" and value is not None:
        for v, t in ty.get("values", []):
            if v == value:
                return t
    return None


def _render_param(p, types, level, order="big"):
    o = {"id": p["id"], "text": p["text"]}
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
    if p.get("storage") == "memory" and size:
        o["extract"] = extract_rule(p.get("bit_offset", 0), size, order)
    ty = types.get(p.get("type")) if p.get("type") else None
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
    if level < 3 and p.get("when"):
        o["when"] = p["when"] if level == 0 else p["when"][-1:]
    if level < 2 and p.get("block"):
        o["block"] = p["block"]
    if p.get("ui") is False:
        o["ui"] = False
    if level == 0 and p.get("variants"):
        o["variants"] = p["variants"]
    return o


def tool_parameters(store, args, budget):
    rec = need_app(store, args)
    query = _arg_str(args, "query")
    address = parse_address(args["address"]) if args.get("address") is not None else None
    include_virtual = _arg_bool(args, "include_virtual", False)
    offset = _arg_int(args, "offset", 0, 0, 1 << 30)
    limit = _arg_int(args, "limit", 25, 1, 200)
    items = []
    for p in rec.get("parameters", []):
        if not include_virtual and not p.get("storage"):
            continue
        if address is not None:
            a = p.get("address")
            if a is None or not a <= address < a + _param_nbytes(p):
                continue
        if query:
            fields = [p.get("text"), p.get("text_de"), p.get("name"), p.get("block"), p.get("id")]
            fields += [v.get("text") for v in p.get("variants", [])]
            if not _words_match(query, fields):
                continue
        items.append(p)
    types = rec.get("types", {})
    bo = rec.get("byte_order", {})
    order = bo.get("used", "big")
    header = {"app_id": rec["app_id"], "name": rec["name"],
              "byte_order": order,
              "note": SLOT_NOTE + " bit_offset counts from the MSB; value = (integer of "
                      "extract.bytes bytes at address in byte_order >> shift) & mask. Union "
                      "members share memory: only the one matching the channel mode is valid."}
    if bo.get("warning"):
        header["byte_order_warning"] = bo["warning"]
    if address is not None:
        header["address"] = address
        header["address_hex"] = "0x%04X" % address
    return _page(items, offset, limit, lambda p, lv: _render_param(p, types, lv, order),
                 header, "parameters", budget)


def tool_decode(store, args, budget):
    rec = need_app(store, args)
    if args.get("address") is None:
        raise ToolError("invalid_argument", "address is required")
    base = parse_address(args["address"])
    hexdata = args.get("data")
    if not isinstance(hexdata, str):
        raise ToolError("invalid_argument", "data must be a hex string, e.g. '0A1B2C'")
    clean = re.sub(r"[\s:,_-]", "", hexdata)
    if clean.lower().startswith("0x"):
        clean = clean[2:]
    if not clean or len(clean) % 2 or not re.match(r"^[0-9A-Fa-f]+$", clean) or len(clean) > 2048:
        raise ToolError("invalid_argument", "data must be an even number of hex digits "
                                            "(1..1024 bytes)")
    data = bytes.fromhex(clean)
    end = base + len(data)
    offset = _arg_int(args, "offset", 0, 0, 1 << 30)
    limit = _arg_int(args, "limit", 40, 1, 200)
    types = rec.get("types", {})
    bo = rec.get("byte_order", {})
    order = bo.get("used", "big")
    values, partial = [], []
    for p in rec.get("parameters", []):
        a, size = p.get("address"), p.get("size_bits")
        if a is None or not size:
            continue
        nb = _param_nbytes(p)
        if a + nb <= base or a >= end:
            continue
        if a < base or a + nb > end:
            partial.append(p["id"])
            continue
        ty = types.get(p.get("type")) if p.get("type") else None
        o = {"id": p["id"], "text": p["text"], "address_hex": "0x%04X" % a,
             "bit_offset": p.get("bit_offset"), "size_bits": size}
        kind = ty.get("kind") if ty else None
        v = None
        if kind in ("number", "enum") and size <= 64:
            v = extract_bits(data, a - base, p.get("bit_offset", 0), size, order)
        if v is not None:
            if kind == "number" and ty.get("signed") and v >= 1 << (size - 1):
                v -= 1 << size
            o["value"] = v
            if kind == "enum":
                vt = _enum_text(ty, str(v))
                o["value_text"] = vt if vt is not None else "(not an enumerated value)"
            if p.get("default") is not None:
                o["default"] = p["default"]
        else:
            o["raw_hex"] = data[a - base:a - base + nb].hex().upper()
            o["kind"] = kind
        if p.get("union"):
            o["union"] = True
        if p.get("when"):
            o["when"] = p["when"]
        values.append(o)
    header = {"app_id": rec["app_id"], "address": base, "address_hex": "0x%04X" % base,
              "length": len(data), "byte_order": order,
              "note": "Union members are all decoded: only the one whose 'when' matches the "
                      "configured channel mode is meaningful. " + SLOT_NOTE}
    if bo.get("warning"):
        header["byte_order_warning"] = bo["warning"]
    if partial:
        header["partial"] = partial[:20]
        if len(partial) > 20:
            header["partial_total"] = len(partial)

    def render(o, level):
        if level == 0:
            return o
        r = {k: v for k, v in o.items() if k != "when"}
        if level == 1 and o.get("when"):
            r["when"] = o["when"][-1:]
        return r

    return _page(values, offset, limit, render, header, "values", budget)


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
         "(channel mode) that shows it; text/function_text/dpt only where they differ. A "
         "name labels the SLOT, not the function: in another mode the same number may do "
         "something else (a 'slat position' slot can switch a pump). Pick the variant "
         "whose size_bits matches the size the device reports. Filter: numbers, query. "
         "Paginated."),
     "inputSchema": {"type": "object", "properties": dict({
         "app_id": _APP_ID_PROP,
         "numbers": {"type": "array", "items": {"type": "integer", "minimum": 0,
                                                "maximum": 65535}},
         "query": {"type": "string"}}, **_PAGE_PROPS), "required": ["app_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "prod-parameters",
     "description": (
         "Parameters of an application program with their storage: address (segment base "
         "+ offset), bit_offset (from the MSB), size_bits, extract (read `bytes` bytes at "
         "address in byte_order, >> shift, & mask), type (enum / min-max / text), "
         "default, union, 'when' (channel mode that shows it). Union members share "
         "memory; only the one matching the mode is valid. Names label slots, not "
         "functions. Filter: query (words) or address (parameters occupying that byte); "
         "include_virtual adds ETS-only parameters without storage. Paginated."),
     "inputSchema": {"type": "object", "properties": dict({
         "app_id": _APP_ID_PROP,
         "query": {"type": "string"},
         "address": _ADDR_PROP,
         "include_virtual": {"type": "boolean"}}, **_PAGE_PROPS), "required": ["app_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "prod-decode",
     "description": (
         "Decode bytes read from device memory (starting at address) into parameter "
         "values: MSB-first bit offsets, the program's byte order. Only parameters fully "
         "inside the bytes; cut-off ones are listed in 'partial'. All union members are "
         "decoded: 'when' says which applies. Pure computation, no device access. "
         "Paginated."),
     "inputSchema": {"type": "object", "properties": dict({
         "app_id": _APP_ID_PROP,
         "address": _ADDR_PROP,
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
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": SERVER_NAME, "version": VERSION},
                  "instructions": INSTRUCTIONS}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
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
                apps = self.server.store.index().get("apps", {})
                files = self.server.store.files()
                self._json(200, {"ok": True, "version": VERSION, "apps": len(apps),
                                 "files": len(files),
                                 "file_errors": sum(1 for r in files.values()
                                                    if r.get("status") == "error")})
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
        idx = self.server.store.index().get("apps", {})
        entries = [idx[a] for a in res["apps_added"] if a in idx]
        reply = {"ok": True, "file": res["file"], "saved_as": res["file"],
                 "stored": res["stored"], "apps_added": res["apps_added"],
                 "apps_new": res.get("apps_new", []), "apps_removed": res.get("apps_removed", []),
                 "products": res.get("products",
                                     len({o for e in entries for o in e.get("order_numbers", [])})),
                 "apps_total": len(idx),
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


def make_server(bind, port, data_dir, products_dir, allowed_hosts=(), upload_origins=None,
                allow_any_bind=False, activate=True):
    """Create the HTTP server and bind its socket.  With activate=False it does
    not accept connections yet (Service listens only after the first sync)."""
    b = (bind or "").strip()
    if b.startswith("[") and b.endswith("]"):
        b = b[1:-1]
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

    def initial_sync(self):
        interval = self.cfg.get("scan_interval", DEFAULT_SCAN_INTERVAL)
        s = sync_products(self.products, self.data,
                          settle=SETTLE_SECONDS if interval > 0 else 0, log=self.log)
        self.log("%s product directory %s: %d file(s), %d application program(s), "
                 "%d error(s), %d pending, %.1f s" % (
                     _utc_now(), self.products, s["files_total"], s["apps_total"],
                     len(s["errors"]), len(s["pending"]), s["seconds"]))
        return s

    def activate(self):
        self.httpd.server_activate()

    def start_scanner(self):
        interval = self.cfg.get("scan_interval", DEFAULT_SCAN_INTERVAL)
        if interval > 0:
            self.scanner = ProductScanner(self.products, self.data, interval, self.log)
            self.scanner.start()

    def close(self):
        if self.scanner:
            self.scanner.stop_event.set()
        self.httpd.server_close()


def serve(cfg):
    svc = Service(cfg)
    svc.initial_sync()
    svc.activate()
    svc.start_scanner()

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
        svc.log("%s knxprod-mcp stopped" % _utc_now())
    return 0


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
    for p in (ps, py):
        p.add_argument("--products", help="product directory, scanned recursively for "
                                          ".knxprod/.zip (KNXPROD_DIR, default %s)"
                                          % DEFAULT_PRODUCTS)
        p.add_argument("--data", help="index directory (KNXPROD_DATA, default %s)" % DEFAULT_DATA)
    ps.add_argument("--bind", help="listen address (KNXPROD_BIND, default %s)" % DEFAULT_BIND)
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
    return ap


def resolve_config(args):
    """Command-line values win over environment variables, which win over defaults."""
    cfg = {"cmd": args.cmd,
           "products": args.products or _env("KNXPROD_DIR") or DEFAULT_PRODUCTS,
           "data": args.data or _env("KNXPROD_DATA") or DEFAULT_DATA}
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
    return cfg


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
        if cfg["cmd"] == "serve":
            return serve(cfg)
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
