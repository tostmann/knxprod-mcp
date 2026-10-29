#!/usr/bin/env python3
"""Tests for knxprod_mcp.py.  Standard library only:

    python3 tests/test_knxprod_mcp.py            (or: python3 -m unittest discover -s tests)

The fixture under tests/fixture/ is synthetic (fictitious manufacturer M-FFF0)
and written for these tests.  HTTP tests listen on 127.0.0.1 with a free port.

Optional live mDNS check on a LAN address of this host (sends multicast):

    KNXPROD_TEST_MDNS_ADDRESS=192.168.1.10 python3 tests/test_knxprod_mcp.py TestMdnsLive

Optional test against a real product file you own (never committed):

    KNXPROD_TEST_REAL_DATA=/path/to/file.knxprod python3 tests/test_knxprod_mcp.py
      KNXPROD_TEST_APP_ID=<10 hex digits>     an ApplicationId inside that file
      KNXPROD_TEST_OBJECTS=0,2,16             com-object numbers it must return

Optional check of ETS module instances in a real product file you own:

    KNXPROD_TEST_MODULE_DATA=/path/to/file.knxprod \
    KNXPROD_TEST_MODULE_APPS=<app id>[,<app id>...] python3 tests/test_knxprod_mcp.py TestRealModules
"""

import base64
import collections
import contextlib
import http.client
import json
import os
import re
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import knxprod_mcp as K  # noqa: E402

FIXTURE = os.path.join(HERE, "fixture")
APP_XML = os.path.join(FIXTURE, "M-FFF0", "M-FFF0_A-1234-56-ABCD.xml")
APP2_XML = os.path.join(FIXTURE, "M-FFF0", "M-FFF0_A-00AB-01-1111-OFFF1.xml")
APP3_XML = os.path.join(FIXTURE, "M-FFF0", "M-FFF0_A-0C0D-21-5A5A.xml")
REAL = os.environ.get("KNXPROD_TEST_REAL_DATA")
REAL_APP = os.environ.get("KNXPROD_TEST_APP_ID")
REAL_OBJECTS = [int(x) for x in (os.environ.get("KNXPROD_TEST_OBJECTS") or "").split(",")
                if x.strip()]
REAL_MODULES = os.environ.get("KNXPROD_TEST_MODULE_DATA")
REAL_MODULE_APPS = [K.normalize_app_id(x) for x in
                    (os.environ.get("KNXPROD_TEST_MODULE_APPS") or "").split(",") if x.strip()]
APP1 = "FFF0123456"
APP2 = "FFF000AB01"
APP3 = "FFF00C0D21"           # built from ETS modules
ENV_KEYS = ("KNXPROD_DIR", "KNXPROD_DATA", "KNXPROD_BIND", "KNXPROD_PORT",
            "KNXPROD_ALLOWED_HOSTS", "KNXPROD_UPLOAD_ORIGINS", "KNXPROD_SCAN_INTERVAL",
            "KNXPROD_ALLOW_ANY_BIND", "KNXPROD_MDNS", "KNXPROD_MDNS_NAME", "KNXPROD_MDNS_HOSTNAME",
            "KNXPROD_MDNS_ADDRESS")


def _tmpdir():
    return tempfile.mkdtemp(prefix="knxprod-mcp-test-")


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def members(*parts, app2_name=None):
    """Fixture files for a synthetic product file: 'master', 'hardware',
    'app1', 'app2'.  app2_name changes the English name of program 2."""
    app2 = _read(APP2_XML)
    if app2_name:
        app2 = app2.replace(b'Text="Test actuator 00AB"', ('Text="%s"' % app2_name).encode())
    table = {"master": ("knx_master.xml", _read(os.path.join(FIXTURE, "knx_master.xml"))),
             "hardware": ("M-FFF0/Hardware.xml", _read(os.path.join(FIXTURE, "M-FFF0",
                                                                    "Hardware.xml"))),
             "app1": ("M-FFF0/M-FFF0_A-1234-56-ABCD.xml", _read(APP_XML)),
             "app2": ("M-FFF0/M-FFF0_A-00AB-01-1111-OFFF1.xml", app2),
             "app3": ("M-FFF0/M-FFF0_A-0C0D-21-5A5A.xml", _read(APP3_XML))}
    return [table[p] for p in parts]


def zip_bytes(items):
    path = tempfile.mktemp(prefix="knxprod-mcp-zip-")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items:
            z.writestr(name, data)
    data = _read(path)
    os.remove(path)
    return data


def write_file(path, data, age=60):
    """Write a product file whose mtime lies `age` seconds in the past."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    t = time.time() - age
    os.utime(path, (t, t))


FULL = None


def full_knxprod():
    global FULL
    if FULL is None:
        FULL = zip_bytes(members("master", "hardware", "app1", "app2"))
    return FULL


def make_knxprod(dst_dir, outer=True):
    """The fixture as a .knxprod, optionally wrapped in an outer zip."""
    knxprod = os.path.join(dst_dir, "fixture.knxprod")
    with open(knxprod, "wb") as f:
        f.write(full_knxprod())
    if not outer:
        return knxprod
    outer_zip = os.path.join(dst_dir, "fixture.knxprod.zip")
    with zipfile.ZipFile(outer_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(knxprod, "Synthetic_Test.knxprod")
    return outer_zip


def by_id(items, ident):
    return [x for x in items if x["id"] == ident][0]


@contextlib.contextmanager
def env(**values):
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    try:
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in values.items()})
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Server:
    """The real service in a thread; by default on 127.0.0.1, a free port and
    without mDNS."""

    def __init__(self, products, data, **cfg):
        self.lines = []
        c = {"products": products, "data": data, "bind": "127.0.0.1", "port": 0,
             "scan_interval": 0}
        c.update(cfg)
        self.svc = K.Service(c, log=self.lines.append)
        self.svc.initial_sync()
        self.svc.activate()
        self.svc.start_scanner()
        self.svc.start_mdns()
        self.port = self.svc.port
        host = self.svc.httpd.server_address[0]
        self.host = "127.0.0.1" if K.is_any_address(host) else host
        self.stopped = False
        self.thread = threading.Thread(target=self.svc.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.svc.httpd.shutdown()
        self.svc.close()
        self.thread.join(5)

    def request(self, method, path, body=b"", headers=None, host=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=120)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host if host is not None else "%s:%d" % (self.host, self.port))
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            if method in ("POST", "PUT") or body:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body or None)
            r = conn.getresponse()
            return r.status, r.headers, r.read()
        finally:
            conn.close()

    def mcp(self, payload, headers=None):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        h.update(headers or {})
        return self.request("POST", "/mcp", json.dumps(payload).encode(), h)

    def tool(self, name, args, rid=1):
        status, _, body = self.mcp({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                    "params": {"name": name, "arguments": args}})
        assert status == 200, (status, body[:300])
        res = json.loads(body)["result"]
        return res, json.loads(res["content"][0]["text"]), len(body)

    def health(self):
        return json.loads(self.request("GET", "/healthz")[2])


def wait_for(cond, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


# ---------------------------------------------------------------------------
# 1. the four parser traps, each with a negative control proving the fixture
#    really contains the trap
# ---------------------------------------------------------------------------

class TestParserTraps(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(APP_XML, "rb") as f:
            raws, cls.langs = K.parse_application_xml(f)
        cls.raw = raws[0]
        cls.params = {p["id"].rsplit("_", 1)[-1]: p for p in cls.raw.params}

    def test_trap1_memory_under_union(self):
        union_members = [p for p in self.raw.params if "union" in p]
        self.assertEqual(len(union_members), 4)
        for p in union_members:
            self.assertIsNotNone(p["loc"], p["id"])
        self.assertEqual(self.params["UP-10"]["loc"]["offset"], 100)
        self.assertEqual(self.params["UP-11"]["loc"]["offset"], 100)
        self.assertEqual(self.params["UP-11"]["loc"]["bit_offset"], 3)
        self.assertEqual(self.params["UP-12"]["loc"]["offset"], 101)
        # union at byte 47 bit 3 + member BitOffset 1 -> byte 47 bit 4
        self.assertEqual(self.params["UP-20"]["loc"]["offset"], 47)
        self.assertEqual(self.params["UP-20"]["loc"]["bit_offset"], 4)

    def test_trap1_negative_control_parameter_memory_only(self):
        root = ET.parse(APP_XML).getroot()
        naive = [p for p in root.iter() if p.tag.endswith("}Parameter")
                 and any(c.tag.endswith("}Memory") for c in p)]
        self.assertEqual(len(naive), 5)
        self.assertEqual(sum(1 for p in self.raw.params if p["loc"]), 10)

    def test_trap2_bit_offset_counts_from_msb(self):
        self.assertEqual(K.extract_bits(b"\x80", 0, 0, 1), 1)      # BitOffset 0 = MSB
        self.assertEqual(K.extract_bits(b"\x01", 0, 7, 1), 1)      # BitOffset 7 = LSB
        self.assertEqual(K.extract_bits(b"\x01", 0, 6, 2), 1)
        self.assertEqual(K.extract_bits(b"\x08", 0, 4, 2), 2)
        self.assertEqual(K.extract_bits(b"\x01\x2c", 0, 0, 16), 300)
        self.assertEqual(K.extract_rule(0, 1), {"bytes": 1, "shift": 7, "mask": "0x1"})
        self.assertEqual(K.extract_rule(6, 2), {"bytes": 1, "shift": 0, "mask": "0x3"})
        self.assertEqual(K.extract_rule(4, 2), {"bytes": 1, "shift": 2, "mask": "0x3"})
        rec = K.build_record(self.raw, self.langs, {"manufacturers": {}, "masks": {}},
                             {"programs": {}, "products": {}}, {})
        self.assertEqual(rec["counts"]["template_check"], {"checked": 6, "match": 6})
        self.assertEqual(rec["byte_order"]["bit_order_evidence"], {"msb_first": 3, "lsb_first": 0})
        self.assertEqual(rec["byte_order"]["template_evidence"], {"big": 1, "little": 0})

    def test_trap3_no_regex_offset(self):
        with open(APP_XML, encoding="utf-8") as f:
            line = [ln for ln in f if 'Offset="66"' in ln][0]
        self.assertEqual(re.search(r'Offset="(\d+)"', line).group(1), "6")
        self.assertEqual(self.params["P-1"]["loc"]["offset"], 66)
        self.assertEqual(self.params["P-1"]["loc"]["bit_offset"], 6)

    def test_trap4_iterparse_clear(self):
        naive = []
        for _, el in ET.iterparse(APP_XML, events=("end",)):
            if el.tag.endswith("}Union"):
                mem = [c for c in el if c.tag.endswith("}Memory")]
                naive.append(mem[0].get("Offset") if mem else None)
                naive.append(([c for c in el if c.tag.endswith("}Parameter")][0]).get("BitOffset"))
            el.clear()
        self.assertEqual(naive, [None, None, None, None])
        self.assertEqual(self.params["UP-10"]["loc"]["segment"], "M-FFF0_A-1234-56-ABCD_AS-4A00")
        self.assertEqual(self.params["UP-20"]["loc"]["bit_offset"], 4)


# ---------------------------------------------------------------------------
# 2. distillation: input forms and values
# ---------------------------------------------------------------------------

class TestDistill(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = _tmpdir()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.data = os.path.join(cls.tmp, "data")
        cls.summary = K.ingest_file(make_knxprod(cls.tmp), cls.data)
        with open(os.path.join(cls.data, "apps", APP1 + ".json")) as f:
            cls.rec = json.load(f)
        with open(os.path.join(cls.data, "apps", APP2 + ".json")) as f:
            cls.rec2 = json.load(f)

    def test_summary(self):
        s = self.summary
        self.assertEqual(s["apps_added"], [APP2, APP1])
        self.assertEqual(s["products"], 3)
        self.assertEqual(s["parameters"], 12)
        self.assertEqual(s["comobjects"], 3)
        self.assertEqual(s["warnings"], [])

    def test_three_input_forms_give_identical_records(self):
        for kind in ("knxprod", "directory"):
            data = os.path.join(self.tmp, "data-" + kind)
            src = make_knxprod(self.tmp, outer=False) if kind == "knxprod" else FIXTURE
            K.ingest_file(src, data)
            for app in (APP1, APP2):
                with open(os.path.join(data, "apps", app + ".json")) as f:
                    other = json.load(f)
                mine = self.rec if app == APP1 else self.rec2
                for key in ("parameters", "comobjects", "segments", "products", "load_procedure",
                            "tables", "types", "counts", "byte_order"):
                    self.assertEqual(other[key], mine[key], "%s %s %s" % (kind, app, key))

    def test_application_info(self):
        r = self.rec
        self.assertEqual(r["app_id"], APP1)
        self.assertEqual(r["id"], "M-FFF0_A-1234-56-ABCD")
        self.assertEqual(r["manufacturer"], {"id": "M-FFF0", "knx_id": 0xFFF0,
                                             "name": "Synthetic Test Manufacturer"})
        self.assertEqual((r["application_number"], r["application_version"]), (0x1234, 0x56))
        self.assertEqual(r["name"], "Test Blind/Switch 1234/5.6")
        self.assertEqual(r["name_de"], "Test Jalousie/Schalten 1234/5.6")
        self.assertEqual(r["mask"], {"id": "MV-0701", "version_hex": "0x0701", "name": "7.1",
                                     "management_model": "BimM112"})
        self.assertEqual(r["load_procedure_style"], "ProductProcedure")
        self.assertEqual([p["order_number"] for p in r["products"]], ["TEST-0001", "TEST-0002"])
        self.assertEqual(r["products"][0]["text"], "Test actuator 2-gang")
        self.assertEqual(r["products"][0]["text_de"], "Testaktor 2fach")
        self.assertTrue(r["source"]["member"].startswith("fixture.knxprod.zip!"))

    def test_segments_tables_load_procedure(self):
        r = self.rec
        seg = {s["id"]: s for s in r["segments"]}
        self.assertEqual(seg["AS-4A00"]["address"], 18944)
        self.assertEqual(seg["AS-4A00"]["lsm"], 3)
        self.assertEqual(seg["AS-4000"]["lsm"], 1)
        self.assertEqual(seg["AS-4000"]["access"], 242)
        self.assertNotIn("lsm", seg["AS-4200"])
        self.assertEqual(r["tables"]["address_table"]["address_hex"], "0x4000")
        self.assertEqual(r["tables"]["address_table"]["max_entries"], 16)
        self.assertEqual(r["tables"]["association_table"]["address"], 16896)
        self.assertEqual(r["tables"]["comobject_table"]["address"], 17408)
        steps = r["load_procedure"]["steps"]
        self.assertEqual(r["load_procedure"]["source"], "application")
        self.assertEqual(steps[0], "Connect")
        self.assertIn("AbsSegment LsmIdx=1 SegType=0 Address=0x4000 Size=32 Access=242 MemType=3 "
                      "SegFlags=128", steps)
        self.assertEqual(steps[-1], "Disconnect")
        self.assertEqual(len(steps), 11)

    def test_parameters(self):
        ps = self.rec["parameters"]
        p1 = by_id(ps, "P-1")
        self.assertEqual((p1["address"], p1["bit_offset"], p1["size_bits"]), (19010, 6, 2))
        self.assertEqual(p1["text"], "Channel 1 operating mode")
        self.assertEqual(p1["text_de"], "Betriebsart Kanal 1")
        self.assertNotIn("name", p1)
        self.assertEqual(p1["variants"], [{"text": "Operating mode of channel 1 (expert)",
                                           "ref": "R-2"}])
        self.assertEqual(p1["block"], "Channel 1")
        p2 = by_id(ps, "P-2")
        self.assertEqual((p2["address"], p2["bit_offset"], p2["size_bits"]), (19011, 0, 1))
        p3 = by_id(ps, "P-3")
        self.assertEqual((p3["address"], p3["size_bits"], p3["default"]), (18954, 16, "300"))
        self.assertEqual(p3["variants"], [{"default": "120", "ref": "R-4"}])
        p5 = by_id(ps, "P-5")
        self.assertEqual(p5["text"], "Frost protection temperature")
        self.assertEqual(self.rec["types"][p5["type"]],
                         {"kind": "number", "size_bits": 8, "signed": True, "min": -20, "max": 40})
        self.assertEqual(p5["ui"], False)
        p6 = by_id(ps, "P-6")
        self.assertEqual(p6["storage"], "property")
        self.assertEqual(p6["property"], {"offset": 1, "bit_offset": 2, "object_index": 5,
                                          "pid": 51, "occurrence": 0})
        self.assertNotIn("address", p6)
        p7 = by_id(ps, "P-7")
        self.assertNotIn("storage", p7)
        self.assertEqual(p7["access"], "None")
        up10, up11, up12, up20 = (by_id(ps, i) for i in ("UP-10", "UP-11", "UP-12", "UP-20"))
        self.assertEqual((up10["address"], up10["bit_offset"]), (19044, 0))
        self.assertEqual((up11["address"], up11["bit_offset"], up11["size_bits"]), (19044, 3, 1))
        self.assertEqual(up12["address"], 19045)
        self.assertEqual((up20["address"], up20["bit_offset"], up20["size_bits"]), (18991, 4, 2))
        self.assertEqual(up10["union"], {"size_bits": 16, "default": True})
        self.assertEqual(up11["union"], {"size_bits": 16, "default": False})
        self.assertEqual(up10["when"], ["Channel 1 operating mode = 1 (Blind)"])
        self.assertEqual(up11["when"], ["Channel 1 operating mode = 2 (Switch)"])
        enum = self.rec["types"][p1["type"]]
        self.assertEqual(enum["values"], [["0", "Off"], ["1", "Blind"], ["2", "Switch"]])
        addrs = [p.get("address") for p in ps if p.get("address") is not None]
        self.assertEqual(addrs, sorted(addrs))
        self.assertEqual(ps[-1]["id"], "P-7")

    def test_comobjects_with_all_variants(self):
        cos = {c["number"]: c for c in self.rec["comobjects"]}
        c1 = cos[1]
        self.assertEqual((c1["text"], c1["text_de"]), ("Slat position", "Position Lamelle"))
        self.assertEqual((c1["function_text"], c1["function_text_de"]), ("Channel 1", "Kanal 1"))
        self.assertEqual((c1["size"], c1["size_bits"], c1["flags"], c1["dpt"]),
                         ("1 Byte", 8, "C-WT--", "5.001"))
        self.assertEqual(len(c1["variants"]), 2)
        v2, v3 = c1["variants"]
        self.assertEqual((v2["ref"], v2["size_bits"], v2["flags"], v2["text"]),
                         ("R-2", 8, "C-WT--", "Slat position"))
        self.assertEqual(v2["when"], ["Channel 1 operating mode = 1 (Blind)"])
        self.assertEqual((v3["ref"], v3["size"], v3["size_bits"], v3["flags"], v3["dpt"]),
                         ("R-3", "1 Bit", 1, "CRW---", "1.001"))
        self.assertEqual((v3["text"], v3["text_de"]), ("Switch pump", "Pumpe schalten"))
        self.assertEqual(v3["when"], ["Channel 1 operating mode = 2 (Switch)"])

    def test_every_place_the_ui_shows_an_item_counts(self):
        # Real programs show one status parameter through four refs, one per
        # channel mode and status setting; the first ref alone named a single
        # combination and contradicted the status object that depends on it.
        ps = self.rec["parameters"]
        p4 = by_id(ps, "P-4")                       # two refs, one per scene setting
        self.assertEqual(p4["when"], ["Channel 1 operating mode = 1 (Blind)"])
        self.assertEqual(p4["when_any"], [["Scene 1 active = 0 (disabled)"],
                                          ["Scene 1 active = 1 (enabled)"]])
        self.assertNotIn("ui", p4)
        self.assertEqual(p4["seen"], 2)
        p6 = by_id(ps, "P-6")                       # one ref, shown in both modes
        self.assertNotIn("when", p6)
        self.assertEqual(p6["when_any"], [["Channel 1 operating mode = 1 (Blind)"],
                                          ["Channel 1 operating mode = 2 (Switch)"]])
        self.assertEqual(p6["seen"], 2)
        o0 = {v["ref"]: v for v in [c for c in self.rec["comobjects"]
                                    if c["number"] == 0][0]["variants"]}
        self.assertNotIn("when", o0["R-1"])
        self.assertNotIn("when_any", o0["R-1"])
        self.assertEqual(o0["R-4"]["when_any"], p6["when_any"])
        self.assertEqual(o0["R-4"]["seen"], 2)
        up10 = by_id(ps, "UP-10")                   # one place: unchanged
        self.assertNotIn("when_any", up10)

    def test_when_fields(self):
        f = K._when_fields
        self.assertEqual(f([]), {})
        self.assertEqual(f([[]]), {})
        self.assertEqual(f([["a"], ["a"]]), {"when": ["a"]})
        self.assertEqual(f([["a", "b"], ["a"]]), {"when": ["a"]})       # "a" alone suffices
        self.assertEqual(f([["a"], ["b"]]), {"when_any": [["a"], ["b"]]})
        self.assertEqual(f([["m", "x"], ["m", "y", "z"]]),
                         {"when": ["m"], "when_any": [["x"], ["y", "z"]]})
        self.assertEqual(f([["a", "s"], ["b", "s"]]),                  # shared, not leading
                         {"when": ["s"], "when_any": [["a"], ["b"]]})
        self.assertEqual(f([["a", "s"], ["s", "a"]]), {"when": ["a", "s"]})
        many = f([["m", "v%d" % i] for i in range(K.MAX_ALTS + 1)])
        self.assertEqual((many["when"], len(many["when_any"]), many["when_any_more"]),
                         (["m"], K.MAX_ALTS, True))
        short = {}
        K._put_when(short, {"when": ["m", "n"], "when_any": [["a", "x"], ["b", "x"], ["y"]]}, 1)
        self.assertEqual(short, {"when": ["n"], "when_any": [["x"], ["y"]]})

    def test_second_program(self):
        r = self.rec2
        self.assertEqual(r["app_id"], APP2)
        self.assertEqual(r["aliases"], ["FFF100AB01"])
        self.assertEqual(r["original_manufacturer"]["name"], "Synthetic OEM Origin")
        self.assertEqual((r["name"], r["name_de"]), ("Test actuator 00AB", "Testaktor 00AB"))
        c = r["comobjects"][0]
        self.assertEqual((c["text"], c["text_de"], c["function_text"]),
                         ("Disable", "Sperren", "Output"))
        p = r["parameters"][0]
        self.assertEqual((p["segment"], p["offset"], p["bit_offset"], p["relative"]),
                         ("RS-04-00000", 2, 7, True))
        self.assertNotIn("address", p)
        self.assertEqual(r["segments"], [{"id": "RS-04-00000", "relative": True, "offset": 0,
                                          "lsm": 4, "size": 16}])
        self.assertEqual(r["load_procedure"]["source"],
                         "knx_master MV-07B0 Load/all merged with application")
        self.assertEqual(r["load_procedure"]["steps"],
                         ["Connect", "RelSegment LsmIdx=4 Size=16 Mode=0 Fill=0 AppliesTo=full",
                          "Restart", "Disconnect"])
        self.assertEqual(r["byte_order"]["declared"], "not stated (BigEndian assumed)")

    def test_cli_sync_uses_environment(self):
        products = os.path.join(self.tmp, "cli-products")
        data = os.path.join(self.tmp, "cli-data")
        write_file(os.path.join(products, "fixture.knxprod"), full_knxprod())
        e = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        e.update({"KNXPROD_DIR": products, "KNXPROD_DATA": data})
        out = subprocess.run([sys.executable, "-I", os.path.join(REPO, "knxprod_mcp.py"), "sync"],
                             capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("added 1", out.stdout)
        with open(os.path.join(data, "index.json")) as f:
            self.assertEqual(sorted(json.load(f)["apps"]), [APP2, APP1])
        write_file(os.path.join(products, "broken.zip"), b"PK\x03\x04 not really a zip")
        out = subprocess.run([sys.executable, "-I", os.path.join(REPO, "knxprod_mcp.py"), "sync",
                              "--products", products, "--data", data],
                             capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(out.returncode, 1)
        self.assertIn("error: broken.zip", out.stdout)


# ---------------------------------------------------------------------------
# 3. zip safety
# ---------------------------------------------------------------------------

class TestZipSafety(unittest.TestCase):

    def setUp(self):
        self.tmp = _tmpdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _zip(self, name, items):
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for n, data in items:
                z.writestr(n, data)
        return path

    def _open(self, path, **kw):
        with K.SourceSet(path, os.path.join(self.tmp, "t"), **kw) as s:
            return [m.name for m in s.members]

    def test_path_traversal_rejected(self):
        for bad in ("../M-FFF0_A-1234-56-ABCD.xml", "/etc/M-FFF0_A-1234-56-ABCD.xml",
                    "a/../../knx_master.xml", "C:/x/Hardware.xml"):
            path = self._zip("bad.zip", [(bad, b"<KNX/>")])
            with self.assertRaises(K.IngestError, msg=bad):
                self._open(path)

    def test_uncompressed_size_cap(self):
        path = self._zip("big.zip", [("knx_master.xml", b" " * 5000)])
        with self.assertRaises(K.IngestError):
            self._open(path, max_total=4000)
        self.assertEqual(len(self._open(path, max_total=6000)), 1)

    def test_member_count_cap(self):
        path = self._zip("many.zip", [("f%d.txt" % i, b"x") for i in range(10)])
        with self.assertRaises(K.IngestError):
            self._open(path, max_members=5)

    def test_nesting_depth_cap(self):
        inner = self._zip("d3.zip", [("knx_master.xml", b"<KNX/>")])
        for level in (2, 1, 0):
            blob = _read(inner)
            inner = self._zip("d%d.zip" % level, [("nested%d.knxprod" % level, blob)])
        with self.assertRaises(K.IngestError):
            self._open(inner, max_depth=2)
        self.assertEqual(len(self._open(inner, max_depth=3)), 1)

    def test_nested_temp_files_removed(self):
        self._open(make_knxprod(self.tmp))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "t")), [])


# ---------------------------------------------------------------------------
# 4. MCP dispatch and tool results (in process)
# ---------------------------------------------------------------------------

class TestMcpDispatch(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = _tmpdir()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.data = os.path.join(cls.tmp, "data")
        K.ingest_file(FIXTURE, cls.data, source="fixture")
        cls.store = K.Store(cls.data)

    def rpc(self, method, params=None, rid=1):
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        return K.handle_rpc(msg, self.store)

    def tool(self, name, args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        self.assertLessEqual(len(K._dumps(r).encode()), K.RESPONSE_BUDGET)
        return r["result"], json.loads(r["result"]["content"][0]["text"])

    def test_initialize_version_negotiation(self):
        for asked, got in (("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"),
                           ("2024-11-05", "2024-11-05"), ("2099-01-01", "2025-06-18"),
                           (None, "2025-06-18")):
            r = self.rpc("initialize", {"protocolVersion": asked, "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "0"}})
            res = r["result"]
            self.assertEqual(res["protocolVersion"], got)
            self.assertEqual(res["serverInfo"]["name"], "knxprod-mcp")
            self.assertIn("tools", res["capabilities"])
            self.assertIn("resources", res["capabilities"])
            for phrase in ("read-only", "never talks to a KNX bus",
                           "START by reading the resource skill://knxprod-mcp/SKILL.md"):
                self.assertIn(phrase, res["instructions"])
            self.assertLess(len(res["instructions"]), 500)          # one short paragraph

    def test_notifications_and_unknown(self):
        self.assertIsNone(K.handle_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"},
                                       self.store))
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertEqual(self.rpc("prompts/list")["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(K.handle_rpc({"id": 1, "method": "ping"}, self.store)["error"]["code"],
                         -32600)

    def test_tools_list(self):
        r = self.rpc("tools/list")
        tools = r["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), 5)
        self.assertTrue(all(n.startswith("prod-") for n in names))
        for t in tools:
            self.assertEqual(t["annotations"], {"readOnlyHint": True})
            self.assertEqual(t["inputSchema"]["type"], "object")
        co = [t for t in tools if t["name"] == "prod-comobjects"][0]
        self.assertIn("SLOT, not the function", co["description"])
        self.assertIn("size_bits", co["description"])
        self.assertLess(len(K._dumps(r).encode()), 5000)

    def test_comobjects_tool(self):
        res, o = self.tool("prod-comobjects", {"app_id": "fff0 12 34 56", "numbers": [1, 9]})
        self.assertFalse(res["isError"])
        self.assertEqual(o["missing_numbers"], [9])
        c = o["comobjects"][0]
        self.assertEqual([v["size_bits"] for v in c["variants"]], [8, 1])
        self.assertEqual(c["variants"][1]["text"], "Switch pump")
        self.assertNotIn("text", c["variants"][0])
        res, o = self.tool("prod-comobjects", {"app_id": APP1, "query": "pump"})
        self.assertEqual([c["number"] for c in o["comobjects"]], [1])
        res, o = self.tool("prod-comobjects", {"app_id": APP1, "numbers": [0]})
        v = {x["ref"]: x for x in o["comobjects"][0]["variants"]}
        self.assertEqual(v["R-4"]["when_any"], [["Channel 1 operating mode = 1 (Blind)"],
                                                ["Channel 1 operating mode = 2 (Switch)"]])

    def test_parameters_tool(self):
        res, o = self.tool("prod-parameters", {"app_id": APP1, "address": "0x4A64"})
        self.assertEqual(sorted(p["id"] for p in o["parameters"]), ["UP-10", "UP-11"])
        up11 = by_id(o["parameters"], "UP-11")
        self.assertEqual(up11["extract"], {"bytes": 1, "shift": 4, "mask": "0x1"})
        self.assertEqual(up11["address_hex"], "0x4A64")
        res, o = self.tool("prod-parameters", {"app_id": APP1, "address": 18955})
        self.assertEqual([p["id"] for p in o["parameters"]], ["P-3"])
        res, o = self.tool("prod-parameters", {"app_id": APP1, "query": "staircase"})
        self.assertEqual([p["id"] for p in o["parameters"]], ["UP-11"])
        res, o = self.tool("prod-parameters", {"app_id": APP1, "query": "channel name"})
        p4 = o["parameters"][0]
        self.assertEqual((p4["id"], p4["when"], p4["when_any"]),
                         ("P-4", ["Channel 1 operating mode = 1 (Blind)"],
                          [["Scene 1 active = 0 (disabled)"], ["Scene 1 active = 1 (enabled)"]]))
        res, o = self.tool("prod-parameters", {"app_id": APP1})
        self.assertEqual(o["total"], 10)
        res, o = self.tool("prod-parameters", {"app_id": APP1, "include_virtual": True})
        self.assertEqual(o["total"], 11)
        res, o = self.tool("prod-parameters", {"app_id": APP1, "limit": 4})
        self.assertEqual((o["returned"], o["next_offset"]), (4, 4))
        res, o = self.tool("prod-parameters", {"app_id": APP1, "offset": 8, "limit": 4})
        self.assertEqual((o["returned"], o["next_offset"]), (2, None))

    def test_decode_tool(self):
        data = [el.text for el in ET.parse(APP_XML).getroot().iter()
                if el.tag.endswith("}Data")][0]
        tpl = base64.b64decode(data)
        res, o = self.tool("prod-decode", {"app_id": APP1, "address": 18944, "data": tpl.hex()})
        self.assertFalse(res["isError"])
        vals = {v["id"]: v for v in o["values"]}
        self.assertEqual((vals["P-1"]["value"], vals["P-1"]["value_text"]), (1, "Blind"))
        self.assertEqual((vals["P-2"]["value"], vals["P-2"]["value_text"]), (1, "enabled"))
        self.assertEqual(vals["P-3"]["value"], 300)
        self.assertEqual(vals["P-5"]["value"], -5)
        self.assertEqual(vals["UP-20"]["value"], 2)
        self.assertEqual(vals["P-4"]["raw_hex"], b"Kitchen\x00\x00\x00".hex().upper())
        self.assertEqual((vals["P-4"]["when"], len(vals["P-4"]["when_any"])),
                         (["Channel 1 operating mode = 1 (Blind)"], 2))
        self.assertTrue(vals["UP-11"]["union"])
        res, o = self.tool("prod-decode", {"app_id": APP1, "address": "0x4A0B", "data": "2C FB"})
        self.assertEqual([v["id"] for v in o["values"]], ["P-5"])
        self.assertEqual(o["partial"], ["P-3"])

    def test_application_and_errors(self):
        res, o = self.tool("prod-application", {"app_id": "M-FFF0_A-1234-56-ABCD"})
        self.assertEqual(o["app_id"], APP1)
        self.assertEqual([p["order_number"] for p in o["products"]], ["TEST-0001", "TEST-0002"])
        self.assertEqual(o["product_files"], ["fixture"])
        res, o = self.tool("prod-application", {"app_id": "FFF100AB01"})   # OEM alias
        self.assertEqual(o["app_id"], APP2)
        res, o = self.tool("prod-application", {"app_id": "FFF0123457"})
        self.assertTrue(res["isError"])
        self.assertEqual(o["error"], "unknown_app_id")
        self.assertEqual([c["app_id"] for c in o["candidates"]], [APP1])
        res, o = self.tool("prod-application", {"app_id": "12345"})
        self.assertTrue(res["isError"])
        self.assertEqual(o["error"], "invalid_argument")
        res, o = self.tool("prod-parameters", {"app_id": APP1, "address": "nowhere"})
        self.assertTrue(res["isError"])
        res, o = self.tool("prod-applications", {"manufacturer": "synthetic"})
        self.assertEqual(o["total"], 3)
        res, o = self.tool("prod-applications", {"query": "TEST-0003"})
        self.assertEqual([a["app_id"] for a in o["applications"]], [APP2])

    def test_budget_paging(self):
        rec = self.store.app(APP1)
        seen, off = [], 0
        while True:
            text = K.tool_parameters(self.store, {"app_id": APP1, "offset": off,
                                                  "include_virtual": True}, 1200)
            self.assertLessEqual(K._esc_len(text), 1200)
            o = json.loads(text)
            seen += [p["id"] for p in o["parameters"]]
            if o["next_offset"] is None:
                break
            off = o["next_offset"]
        self.assertEqual(seen, [p["id"] for p in rec["parameters"]])


# ---------------------------------------------------------------------------
# 4b. ETS modules: a definition instantiated several times (program APP3)
# ---------------------------------------------------------------------------

def segment_data(xml_path, seg_suffix):
    for el in ET.parse(xml_path).getroot().iter():
        if el.tag.endswith("Segment") and el.get("Id", "").endswith(seg_suffix):
            return base64.b64decode([c for c in el if c.tag.endswith("}Data")][0].text)
    raise KeyError(seg_suffix)


def distill_xml(xml):
    """build_record of a program given as XML text (for variants of APP3)."""
    path = tempfile.mktemp(prefix="knxprod-mcp-xml-")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(xml)
        with open(path, "rb") as f:
            raws, langs = K.parse_application_xml(f)
    finally:
        os.remove(path)
    return K.build_record(raws[0], langs, {"manufacturers": {}, "masks": {}},
                          {"programs": {}, "products": {}, "langs": {}}, {})


class TestModules(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = _tmpdir()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.data = os.path.join(cls.tmp, "data")
        src = os.path.join(cls.tmp, "modules.knxprod")
        with open(src, "wb") as f:
            f.write(zip_bytes(members("master", "app3")))
        cls.summary = K.ingest_file(src, cls.data)
        cls.store = K.Store(cls.data)
        cls.rec = cls.store.app(APP3)
        cls.p = {p["id"]: p for p in cls.rec["parameters"]}
        cls.c = {c["id"]: c for c in cls.rec["comobjects"]}

    def tool(self, name, args):
        r = K.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}}, self.store)
        self.assertLessEqual(len(K._dumps(r).encode()), K.RESPONSE_BUDGET)
        return r["result"], json.loads(r["result"]["content"][0]["text"])

    def test_counts(self):
        c = self.rec["counts"]
        self.assertEqual((c["module_defs"], c["module_instances"]), (4, 8))
        self.assertEqual((c["parameters"], c["module_parameters"]), (24, 20))
        self.assertEqual((c["comobjects"], c["module_comobjects"]), (14, 13))
        self.assertEqual(c["template_check"], {"checked": 19, "match": 17})
        self.assertEqual(c["template_check_modules"], {"checked": 16, "match": 14})
        for key in ("module_unsupported", "texts_with_placeholders", "module_records_skipped"):
            self.assertNotIn(key, c)
        self.assertEqual([(m["id"], m["name"], m["parameters"], m["comobjects"], m["instances"])
                          for m in self.rec["modules"]],
                         [("MD-1", "ModuleDefSwitch", 5, 2, 2), ("MD-2", "ModuleDefDimmer", 2, 1, 1),
                          ("MD-3", "ModuleDefSensor", 2, 2, 3), ("MD-4", "ModuleDefScene", 1, 1, 2)])
        self.assertEqual(self.rec["modules"][0]["arguments"],
                         {"ParamOffsBase": 16, "ObjNumberBase": 4, "ChName": "text"})
        self.assertEqual(self.summary["per_app"][0]["module_instances"], 8)
        self.assertFalse([w for w in self.summary["warnings"] if "module" in w])

    def test_addresses_from_base_offset(self):
        p = self.p
        # segment 0x4800; ParamOffsBase 16 (Left) and 32 (Right) from NumericArg
        for pid, addr in (("MD-1_M-1_P-2", 0x4810), ("MD-1_M-1_P-3", 0x4812),
                          ("MD-1_M-1_UP-4", 0x4814), ("MD-1_M-1_UP-5", 0x4814),
                          ("MD-2_M-2_P-1", 0x4810), ("MD-2_M-2_P-2", 0x4812),
                          ("MD-1_M-3_P-2", 0x4820), ("MD-1_M-3_P-3", 0x4822),
                          ("MD-1_M-3_UP-4", 0x4824), ("MD-1_M-3_UP-5", 0x4824),
                          ("MD-4_M-5_MI-1_P-1", 0x4850), ("MD-4_M-5_MI-2_P-1", 0x4858)):
            self.assertEqual((p[pid]["address"], p[pid]["bit_offset"]), (addr, 0), pid)
        self.assertEqual(p["MD-1_M-3_UP-5"]["union"], {"size_bits": 8, "default": False})
        # a <Repeat> with allocators, placed in a relative segment
        for i, off in ((1, 1), (2, 11), (3, 21)):
            q = p["MD-3_M-4_MI-%d_P-1" % i]
            self.assertEqual((q["segment"], q["offset"], q.get("address"), q["relative"]),
                             ("RS-04-00000", off, None, True))
        self.assertNotIn("storage", p["MD-1_M-3_P-1"])               # text parameter, no memory
        # the manufacturer's template holds each instance's defaults right there
        tpl = segment_data(APP3_XML, "_AS-4800")
        for pid in ("MD-1_M-1_P-3", "MD-1_M-3_P-3", "MD-1_M-3_UP-4", "MD-4_M-5_MI-2_P-1"):
            q = p[pid]
            v = K.extract_bits(tpl, q["address"] - 0x4800, q["bit_offset"], q["size_bits"])
            self.assertEqual(v, int(q["default"]), pid)

    def test_comobject_numbers_from_base_number(self):
        got = sorted((c["number"], c["id"]) for c in self.rec["comobjects"])
        self.assertEqual(got, [
            (0, "O-0"), (1, "MD-1_M-1_O-1"), (1, "MD-2_M-2_O-1"), (2, "MD-1_M-1_O-2"),
            (5, "MD-1_M-3_O-1"), (6, "MD-1_M-3_O-2"),
            (20, "MD-3_M-4_MI-1_O-1"), (21, "MD-3_M-4_MI-1_O-2"), (22, "MD-3_M-4_MI-2_O-1"),
            (23, "MD-3_M-4_MI-2_O-2"), (24, "MD-3_M-4_MI-3_O-1"), (25, "MD-3_M-4_MI-3_O-2"),
            (30, "MD-4_M-5_MI-1_O-1"), (31, "MD-4_M-5_MI-2_O-1")])
        ids = [x["id"] for x in self.rec["parameters"] + self.rec["comobjects"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_labels_and_texts(self):
        p, c = self.p, self.c
        self.assertEqual(p["MD-1_M-1_P-2"]["module"], {"id": "MD-1_M-1", "label": "Channel Left"})
        self.assertEqual(p["MD-2_M-2_P-1"]["module"], {"id": "MD-2_M-2", "label": "Channel Left"})
        self.assertEqual(p["MD-1_M-3_P-2"]["module"]["label"], "Channel Right")
        self.assertEqual([p["MD-3_M-4_MI-%d_P-1" % i]["module"]["label"] for i in (1, 2, 3)],
                         ["Sensor 1", "Sensor 2", "Sensor 3"])
        self.assertEqual([p["MD-4_M-5_MI-%d_P-1" % i]["module"]["label"] for i in (1, 2)],
                         ["Scene #1", "Scene #2"])                    # label without argument
        self.assertNotIn("module", p["P-1"])
        # {{argument}} in parameter, object and block texts, translated text argument
        self.assertEqual((p["MD-3_M-4_MI-2_P-1"]["text"], p["MD-3_M-4_MI-2_P-1"]["text_de"]),
                         ("Sensor 2 offset (0.1 K)", "Sensor 2 Offset (0,1 K)"))
        self.assertEqual(p["MD-1_M-3_P-1"]["text"], "Channel Right description")
        self.assertEqual(p["MD-1_M-1_P-3"]["block"], "Channel Left - switching")
        self.assertEqual(c["MD-3_M-4_MI-3_O-1"]["text"], "Sensor 3 temperature")
        # {{0}} = the text parameter (empty by default), {{0:default}}
        v = c["MD-1_M-3_O-1"]["variants"][0]
        self.assertEqual((v["text"], v["text_de"]), ("Channel Right", "Kanal Rechts"))
        # every ref brings its own text: the template's "Switch" is never shown
        self.assertEqual((c["MD-1_M-3_O-1"]["text"], c["MD-1_M-3_O-1"]["text_de"]),
                         ("Channel Right", "Kanal Rechts"))
        self.assertEqual(c["MD-1_M-1_O-1"]["text"], "Channel Left")
        self.assertEqual(c["MD-1_M-1_O-2"]["variants"][0]["text"], "Channel Left status")
        # {{0}} outside modules: the object takes its variant's resolved text
        self.assertEqual((c["O-0"]["text"], c["O-0"]["variants"][0]["text"]),
                         ("Test device", "Test device"))

    def test_conditions(self):
        p = self.p
        self.assertEqual(p["MD-1_M-1_P-3"]["when"], ["Channel Left mode = 1 (switch)",
                                                     "Switch-on delay active = 1 (enabled)"])
        self.assertEqual(p["MD-2_M-2_P-1"]["when"], ["Channel Left mode = 2 (dimmer)"])
        self.assertEqual(p["MD-1_M-3_P-3"]["when"][0], "Channel Right mode = 1 (switch)")
        self.assertEqual(p["MD-3_M-4_MI-3_P-2"]["when"], ["Number of sensors >= 3"])
        self.assertNotIn("when", p["MD-4_M-5_MI-1_P-1"])
        self.assertEqual((p["MD-1_M-1_UP-5"]["ui"], p["MD-1_M-1_UP-5"]["when"]),
                         (False, ["Channel Left mode = 1 (switch)"]))
        self.assertEqual(self.c["MD-3_M-4_MI-2_O-1"]["variants"][0]["when"],
                         ["Number of sensors >= 2"])

    def test_records_are_stable(self):
        data = os.path.join(self.tmp, "again")
        K.ingest_file(FIXTURE, data)                     # other input form, other order
        other = load_json(os.path.join(data, "apps", APP3 + ".json"))
        for key in ("parameters", "comobjects", "counts", "modules", "segments"):
            self.assertEqual(other[key], self.rec[key], key)

    def test_tools(self):
        res, o = self.tool("prod-comobjects", {"app_id": APP3, "numbers": [1, 22]})
        self.assertEqual([(c["number"], c["module"]) for c in o["comobjects"]],
                         [(1, "Channel Left"), (1, "Channel Left"), (22, "Sensor 2")])
        res, o = self.tool("prod-comobjects", {"app_id": APP3, "module": "Sensor 3"})
        self.assertEqual([c["number"] for c in o["comobjects"]], [24, 25])
        res, o = self.tool("prod-comobjects", {"app_id": APP3, "query": "kanal rechts"})
        self.assertEqual([c["number"] for c in o["comobjects"]], [5])
        res, o = self.tool("prod-parameters", {"app_id": APP3, "module": "channel left"})
        self.assertEqual(sorted(p["id"] for p in o["parameters"]), [
            "MD-1_M-1_P-2", "MD-1_M-1_P-3", "MD-1_M-1_UP-4", "MD-1_M-1_UP-5",
            "MD-2_M-2_P-1", "MD-2_M-2_P-2"])
        res, o = self.tool("prod-parameters", {"app_id": APP3, "module": "MD-3_M-4"})
        self.assertEqual(o["total"], 6)                  # every repetition of the <Repeat>
        res, o = self.tool("prod-parameters", {"app_id": APP3, "module": "MD-1"})
        self.assertEqual(o["total"], 8)                  # every instance of the definition
        res, o = self.tool("prod-parameters", {"app_id": APP3, "address": "0x4810"})
        self.assertEqual(sorted((p["id"], p["module"]) for p in o["parameters"]),
                         [("MD-1_M-1_P-2", "Channel Left"), ("MD-2_M-2_P-1", "Channel Left")])
        res, o = self.tool("prod-parameters", {"app_id": APP3, "segment": "rs-04-00000",
                                               "address": 13})
        self.assertEqual([p["id"] for p in o["parameters"]], ["MD-3_M-4_MI-2_P-2"])
        self.assertEqual((o["segment"], o["segment_offset"]), ("RS-04-00000", 13))
        res, o = self.tool("prod-parameters", {"app_id": APP3, "address": 13})
        self.assertEqual(o["total"], 0)
        self.assertIn("RS-04-00000", o["hint"])
        res, o = self.tool("prod-parameters", {"app_id": APP3, "segment": "RS-9", "address": 1})
        self.assertTrue(res["isError"])
        self.assertEqual(o["segments"], ["AS-4800", "RS-04-00000"])
        res, o = self.tool("prod-application", {"app_id": APP3})
        self.assertEqual(o["counts"]["module_instances"], 8)
        self.assertEqual([m["id"] for m in o["modules"]], ["MD-1", "MD-2", "MD-3", "MD-4"])

    def test_decode(self):
        tpl = segment_data(APP3_XML, "_AS-4800")
        res, o = self.tool("prod-decode", {"app_id": APP3, "address": 0x4800, "data": tpl.hex(),
                                           "limit": 200})
        vals = {v["id"]: v for v in o["values"]}
        self.assertEqual((vals["MD-1_M-3_P-3"]["value"], vals["MD-1_M-3_P-3"]["module"],
                          vals["MD-1_M-3_P-3"]["address_hex"]), (300, "Channel Right", "0x4822"))
        self.assertEqual(vals["MD-1_M-1_UP-4"]["value"], 5)
        self.assertEqual(vals["MD-2_M-2_P-1"]["value"], 0x80)       # the alternative module
        self.assertEqual(vals["MD-4_M-5_MI-2_P-1"]["value"], 1)
        rs = segment_data(APP3_XML, "_RS-04-00000")
        res, o = self.tool("prod-decode", {"app_id": APP3, "segment": "RS-04-00000",
                                           "address": 10, "data": rs[10:20].hex()})
        self.assertEqual((o["segment"], o["segment_offset"], o["offset"]), ("RS-04-00000", 10, 0))
        self.assertEqual([(v["id"], v["offset"], v["value"], v["module"]) for v in o["values"]],
                         [("MD-3_M-4_MI-2_P-1", 11, 20, "Sensor 2"),
                          ("MD-3_M-4_MI-2_P-2", 12, 60, "Sensor 2")])
        res, o = self.tool("prod-decode", {"app_id": APP3, "address": 10, "data": "00"})
        self.assertEqual(o["values"], [])
        self.assertIn("RS-04-00000", o["hint"])

    def test_budget_paging(self):
        for tool, key in ((K.tool_parameters, "parameters"), (K.tool_comobjects, "comobjects")):
            seen, off = [], 0
            while True:
                text = tool(self.store, {"app_id": APP3, "offset": off, "include_virtual": True},
                            900)
                self.assertLessEqual(K._esc_len(text), 900)
                o = json.loads(text)
                seen += [x["id"] if key == "parameters" else x["number"] for x in o[key]]
                if o["next_offset"] is None:
                    break
                off = o["next_offset"]
            self.assertEqual(seen, [x["id"] if key == "parameters" else x["number"]
                                    for x in self.rec[key]])

    def test_unsupported_features_are_counted(self):
        xml = _read(APP3_XML).decode("utf-8")
        pre = "M-FFF0_A-0C0D-21-5A5A_"

        def change(old, new):
            nonlocal xml
            self.assertEqual(xml.count(old), 1, old)
            xml = xml.replace(old, new)

        # a module definition nested in MD-1, instantiated inside MD-1
        change('<ModuleDef Id="%sMD-1" Name="ModuleDefSwitch">' % pre,
               '<ModuleDef Id="%sMD-1" Name="ModuleDefSwitch"><SubModuleDefs>'
               '<ModuleDef Id="%sMD-1_SM-1" Name="Sub"><Static><Parameters>'
               '<Parameter Id="%sMD-1_SM-1_P-1" Name="N" Text="Nested parameter" '
               'ParameterType="%sPT-Byte" Value="0"><Memory CodeSegment="%sAS-4800" Offset="0" '
               'BitOffset="0" /></Parameter></Parameters></Static></ModuleDef></SubModuleDefs>'
               % ((pre,) * 5))
        change('<ParameterRefRef RefId="%sMD-1_P-1_R-1" />' % pre,
               '<ParameterRefRef RefId="%sMD-1_P-1_R-1" /><Module Id="%sMD-1_SM-1_M-1" '
               'RefId="%sMD-1_SM-1" />' % (pre, pre, pre))
        # channel Right without its ParamOffsBase argument
        change('<NumericArg RefId="%sMD-1_A-1" Value="32" />' % pre, '')
        # a repetition without a count, an allocator that runs out
        change('Name="Scenes" Count="2"', 'Name="Scenes"')
        change('Name="SensorObjNumber" Start="20" maxInclusive="26"',
               'Name="SensorObjNumber" Start="20" maxInclusive="24"')
        rec = distill_xml(xml)
        c = rec["counts"]
        self.assertEqual(c["module_unsupported"], {
            "allocator_overflow": 1, "module_def_nested": 1, "module_in_module_def": 1,
            "parameters_unplaced": 4, "repeat_count_unknown": 1})
        texts = [p["text"] for p in rec["parameters"]]
        self.assertNotIn("Nested parameter", texts)
        self.assertEqual(c["module_instances"], 5)                   # 3 channels + 2 sensors
        ids = {p["id"] for p in rec["parameters"]}
        self.assertIn("MD-1_M-3_P-1", ids)                           # needs no argument
        self.assertNotIn("MD-1_M-3_P-2", ids)
        self.assertNotIn("MD-3_M-4_MI-3_P-1", ids)
        self.assertEqual(sorted(x["number"] for x in rec["comobjects"] if
                                x["id"].startswith("MD-1_M-3")), [5, 6])   # numbers still known

    def test_placeholder_naming_a_parameter_ref(self):
        # older schemas: {{226}} = the parameter behind ParameterRef ..._R-226
        xml = _read(APP3_XML).decode("utf-8").replace("_P-4_R-4", "_P-4_R-226")
        xml = xml.replace('Text="{{0}}" TextParameterRefId="M-FFF0_A-0C0D-21-5A5A_P-4_R-226"',
                          'Text="Heartbeat: {{226}}"')
        rec = distill_xml(xml)
        o0 = [c for c in rec["comobjects"] if c["id"] == "O-0"][0]
        self.assertEqual((o0["text"], o0["variants"][0]["text"]),
                         ("Heartbeat: Test device", "Heartbeat: Test device"))
        self.assertNotIn("texts_with_placeholders", rec["counts"])

    def test_placeholders(self):
        f = K.fill_placeholders
        names = {"ChNo": "B", "Empty": ""}.get
        self.assertEqual(f("Channel {{ChNo}}: {{Empty}}", names), ("Channel B", 0))
        self.assertEqual(f("{{Empty:Channel {{ChNo}}}} status", names), ("Channel B status", 0))
        self.assertEqual(f("{{Unknown}} and {{ChNo}}", names), ("{{Unknown}} and B", 1))
        self.assertEqual(f("{{Unknown:fallback}}", names), ("fallback", 0))
        self.assertEqual(f("no placeholder", names), ("no placeholder", 0))
        self.assertEqual(f("broken {{ChNo", names), ("broken {{ChNo", 0))


# ---------------------------------------------------------------------------
# 4c. the skill: skill/SKILL.md served as an MCP resource
# ---------------------------------------------------------------------------

SKILL_PATH = os.path.join(REPO, "skill", "SKILL.md")


def dockerignore_keeps(path, patterns):
    """.dockerignore semantics, simplified: the last matching pattern decides;
    a pattern also matches every path below a matching directory."""
    import fnmatch
    keep = True
    parts = path.split("/")
    for pat in patterns:
        neg = pat.startswith("!")
        pat = pat[1:] if neg else pat
        if any(fnmatch.fnmatchcase("/".join(parts[:i]), pat) for i in range(1, len(parts) + 1)):
            keep = neg
    return keep


class TestSkill(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = _read(SKILL_PATH).decode("utf-8")

    def rpc(self, method, params=None):
        msg = {"jsonrpc": "2.0", "id": 7, "method": method}
        if params is not None:
            msg["params"] = params
        return K.handle_rpc(msg, None)

    def test_resources_list_and_read(self):
        r = self.rpc("resources/list")["result"]["resources"]
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["uri"], r[0]["name"], r[0]["mimeType"]),
                         ("skill://knxprod-mcp/SKILL.md", "SKILL.md", "text/markdown"))
        self.assertEqual(r[0]["size"], len(self.text.encode("utf-8")))
        self.assertIn("first", r[0]["description"])
        msg = self.rpc("resources/read", {"uri": K.SKILL_URI})
        self.assertLessEqual(len(K._dumps(msg).encode()), K.RESPONSE_BUDGET)
        c = msg["result"]["contents"]
        self.assertEqual(c, [{"uri": K.SKILL_URI, "mimeType": "text/markdown",
                              "text": self.text}])
        err = self.rpc("resources/read", {"uri": "skill://other/SKILL.md"})["error"]
        self.assertEqual((err["code"], err["data"]), (-32002, {"uri": "skill://other/SKILL.md"}))
        self.assertEqual(self.rpc("resources/templates/list")["result"],
                         {"resourceTemplates": []})

    def test_missing_file(self):
        saved = K.SKILL_FILE
        K.SKILL_FILE = os.path.join(_tmpdir(), "SKILL.md")
        try:
            self.assertEqual(self.rpc("resources/list")["result"]["resources"], [])
            self.assertEqual(self.rpc("resources/read", {"uri": K.SKILL_URI})["error"]["code"],
                             -32002)
        finally:
            shutil.rmtree(os.path.dirname(K.SKILL_FILE), ignore_errors=True)
            K.SKILL_FILE = saved

    def test_content(self):
        t = self.text
        self.assertLessEqual(len(t.encode("utf-8")), 8192)
        for phrase in (
                "ships no manufacturer data", "read-only", "never talks to a KNX bus",
                "object 3, PID 13", "10 hex digits", "knx-prop-read", "prod-applications",
                "several products", "prod-application ", "prod-comobjects", "prod-parameters",
                "knx-mem-read", "prod-decode", "size_bits", "configured mode", "SLOT",
                "MSB", "`union`", "`when`", "`when_any`", "module", "byte_order_warning",
                "load session",
                "dry run", "`offset`", "next_offset", "12 000 bytes", "unknown_app_id",
                "upload", "terms"):
            self.assertIn(phrase, t, phrase)
        self.assertEqual(t.count("\nWhat "), 2)                         # two examples
        tools = {x["name"] for x in K.TOOLS}
        self.assertEqual(set(re.findall(r"\bprod-[a-z]+", t)), tools)
        # synthetic ids only, nothing that looks like a real program or host
        self.assertEqual(set(re.findall(r"\b[0-9A-F]{10}\b", t)), {"FFF0123456"})
        self.assertFalse(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", t))

    def test_image_contains_the_skill(self):
        with open(os.path.join(REPO, "Dockerfile")) as f:
            docker = f.read()
        self.assertIn('ENTRYPOINT ["python", "/app/knxprod_mcp.py"]', docker)
        self.assertRegex(docker, r"(?m)^COPY skill/SKILL\.md /app/skill/$")
        with open(os.path.join(REPO, ".dockerignore")) as f:
            patterns = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        self.assertTrue(dockerignore_keeps("skill/SKILL.md", patterns))
        self.assertTrue(dockerignore_keeps("knxprod_mcp.py", patterns))
        self.assertFalse(dockerignore_keeps("knxprod/Vendor.knxprod", patterns))
        self.assertFalse(dockerignore_keeps("tests/fixture/knx_master.xml", patterns))
        # the server looks for it next to itself: /app/skill/SKILL.md in the image
        self.assertEqual(os.path.relpath(K.SKILL_FILE, os.path.dirname(K.__file__)),
                         os.path.join("skill", "SKILL.md"))

    def test_over_http(self):
        tmp = _tmpdir()
        self.addCleanup(shutil.rmtree, tmp, True)
        srv = Server(os.path.join(tmp, "knxprod"), os.path.join(tmp, "index"))
        self.addCleanup(srv.stop)
        status, _, body = srv.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18"}})
        res = json.loads(body)["result"]
        self.assertIn(K.SKILL_URI, res["instructions"])
        status, _, body = srv.mcp({"jsonrpc": "2.0", "id": 2, "method": "resources/read",
                                   "params": {"uri": K.SKILL_URI}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["contents"][0]["text"], self.text)
        self.assertFalse([ln for ln in srv.lines if "SKILL.md is missing" in ln])


# ---------------------------------------------------------------------------
# 5. configuration: flags, environment, defaults
# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):

    def cfg(self, *argv):
        return K.resolve_config(K.build_parser().parse_args(list(argv)))

    def test_defaults(self):
        with env():
            c = self.cfg("serve")
        self.assertEqual((c["products"], c["data"], c["bind"], c["port"]),
                         ("/knxprod", "/data", "127.0.0.1", 8095))
        self.assertEqual((c["scan_interval"], c["allow_any_bind"], c["upload_origins"],
                          c["allowed_hosts"]), (10.0, False, None, []))
        self.assertEqual(c["mdns"], {"enabled": True, "name": None, "hostname": None,
                                     "address": None})

    def test_environment(self):
        with env(KNXPROD_DIR="/p", KNXPROD_DATA="/d", KNXPROD_BIND="192.0.2.10",
                 KNXPROD_PORT="9001", KNXPROD_ALLOWED_HOSTS="knx-server.example.lan, Other.Lan.",
                 KNXPROD_UPLOAD_ORIGINS="http://192.0.2.20, https://stick.example.lan:8443",
                 KNXPROD_SCAN_INTERVAL="2.5", KNXPROD_ALLOW_ANY_BIND="1"):
            c = self.cfg("serve")
            self.assertEqual((c["products"], c["data"], c["bind"], c["port"]),
                             ("/p", "/d", "192.0.2.10", 9001))
            self.assertEqual(c["allowed_hosts"], ["knx-server.example.lan", "other.lan"])
            self.assertEqual(c["upload_origins"], frozenset({
                ("http", "192.0.2.20", None), ("https", "stick.example.lan", 8443)}))
            self.assertEqual((c["scan_interval"], c["allow_any_bind"]), (2.5, True))
            # flags win over the environment
            c = self.cfg("serve", "--products", "/q", "--port", "8100", "--allowed-host", "x.lan",
                         "--scan-interval", "0", "--upload-origin", "none")
            self.assertEqual((c["products"], c["port"], c["allowed_hosts"], c["scan_interval"],
                              c["upload_origins"]), ("/q", 8100, ["x.lan"], 0.0, frozenset()))
            c = self.cfg("sync")
            self.assertEqual((c["products"], c["data"]), ("/p", "/d"))

    def test_bad_values(self):
        for bad in ({"KNXPROD_PORT": "eighty"}, {"KNXPROD_PORT": "70000"},
                    {"KNXPROD_SCAN_INTERVAL": "soon"}, {"KNXPROD_UPLOAD_ORIGINS": "ftp://x"},
                    {"KNXPROD_ALLOWED_HOSTS": "bad_name!"}):
            with env(**bad):
                with self.assertRaises(K.ConfigError, msg=str(bad)):
                    self.cfg("serve")

    def test_any_address_needs_explicit_permission(self):
        tmp = _tmpdir()
        try:
            for addr in ("0.0.0.0", "::", "[::]", ""):
                with self.assertRaises(K.ConfigError, msg=addr):
                    K.make_server(addr, 0, tmp, tmp)
            httpd = K.make_server("0.0.0.0", 0, tmp, tmp, allow_any_bind=True, activate=False)
            httpd.server_close()
            httpd = K.make_server("::1", 0, tmp, tmp)             # IPv6 works
            self.assertEqual(httpd.address_family, socket.AF_INET6)
            httpd.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_main_reports_config_errors(self):
        with env(KNXPROD_PORT="nope"):
            with open(os.devnull, "w") as dn, contextlib.redirect_stderr(dn):
                self.assertEqual(K.main(["serve"]), 2)


# ---------------------------------------------------------------------------
# 6. Host header, /mcp Origin and upload CORS rules
# ---------------------------------------------------------------------------

class TestGuards(unittest.TestCase):

    def test_host_header(self):
        ok = ("192.168.1.5", "192.168.1.5:8095", "10.0.0.1", "203.0.113.7:80", "[::1]",
              "[::1]:8095", "[fe80::1%25eth0]:8095", "::1", "localhost", "LOCALHOST:8095",
              "knx.local", "knx.local.:8095", "stick-1.local", "a.b.local")
        bad = ("", "evil.example", "evil.example:8095", "192.168.1.5.nip.io", "localhost.evil.com",
               "local", "a..local", "x.local:99999", "[::1", "[::1]x", "host:1:2", "1.2.3.4:abc",
               "_bad.local", "knx-server.example.lan", "0x7f.0.0.1")
        for h in ok:
            self.assertTrue(K.host_allowed(h), h)
        for h in bad:
            self.assertFalse(K.host_allowed(h), h)
        extra = {"knx-server.example.lan"}
        self.assertTrue(K.host_allowed("knx-server.example.lan:8095", extra))
        self.assertTrue(K.host_allowed("KNX-Server.Example.Lan.", extra))
        self.assertFalse(K.host_allowed("other.example.lan", extra))

    def test_mcp_origin(self):
        for o in ("http://192.168.1.5:8080", "http://knx.local", "http://localhost:3000",
                  "https://[fd00::1]", "http://203.0.113.9"):
            self.assertTrue(K.mcp_origin_allowed(o), o)
        for o in ("https://evil.example", "null", "file://", "http://user@192.168.1.5",
                  "http://192.168.1.5/path", ""):
            self.assertFalse(K.mcp_origin_allowed(o), o)
        self.assertTrue(K.mcp_origin_allowed("https://knx-server.example.lan",
                                             {"knx-server.example.lan"}))

    def test_upload_origin_default(self):
        ok = ("http://10.1.2.3", "http://172.16.0.1", "http://172.31.255.255:8080",
              "http://192.168.178.20", "https://192.168.1.2", "http://[fd12:3456::1]",
              "http://[fe80::1]:80", "http://stick.local", "http://localhost:5173",
              "http://127.0.0.1:8000", "http://[::1]")
        bad = ("http://8.8.8.8", "http://172.32.0.1", "http://11.0.0.1", "http://100.64.0.1",
               "http://169.254.1.1", "http://evil.example", "http://192.168.1.2.nip.io",
               "ftp://192.168.1.2", "null", "http://[2001:db8::1]", "http://[::ffff:8.8.8.8]",
               "http://192.168.1.2/x")
        for o in ok:
            self.assertTrue(K.upload_origin_allowed(o), o)
        for o in bad:
            self.assertFalse(K.upload_origin_allowed(o), o)

    def test_upload_origin_override(self):
        only = K.parse_upload_origins(["http://192.168.1.20"])
        self.assertTrue(K.upload_origin_allowed("http://192.168.1.20", only))
        self.assertTrue(K.upload_origin_allowed("http://192.168.1.20:80", only))
        self.assertFalse(K.upload_origin_allowed("http://192.168.1.21", only))
        self.assertFalse(K.upload_origin_allowed("http://stick.local", only))
        nobody = K.parse_upload_origins(["none"])
        self.assertFalse(K.upload_origin_allowed("http://192.168.1.20", nobody))
        self.assertIsNone(K.parse_upload_origins([]))


# ---------------------------------------------------------------------------
# 7. product directory: add, change, delete, subfolders, errors
# ---------------------------------------------------------------------------

class TestProductDirectory(unittest.TestCase):

    def setUp(self):
        self.tmp = _tmpdir()
        self.products = os.path.join(self.tmp, "knxprod")
        self.data = os.path.join(self.tmp, "index")
        os.makedirs(self.products)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sync(self, **kw):
        kw.setdefault("settle", 0)
        return K.sync_products(self.products, self.data, **kw)

    def index(self):
        with open(os.path.join(self.data, "index.json")) as f:
            return json.load(f)["apps"]

    def files_state(self):
        with open(os.path.join(self.data, "files.json")) as f:
            return json.load(f)["files"]

    def put(self, rel, data, age=60):
        write_file(os.path.join(self.products, rel), data, age)

    def test_add_unchanged_touch(self):
        self.put("a.knxprod", full_knxprod())
        s = self.sync()
        self.assertEqual((s["added"], s["apps_total"], s["files_total"]), (["a.knxprod"], 2, 1))
        self.assertEqual(self.index()[APP1]["sources"], ["a.knxprod"])
        self.assertEqual(self.files_state()["a.knxprod"]["sha256"],
                         K._sha256_file(os.path.join(self.products, "a.knxprod")))
        generated = load_json(os.path.join(self.data, "index.json"))["generated"]
        s = self.sync()
        self.assertEqual((s["added"], s["changed"], s["unchanged"]), ([], [], 1))
        t = time.time() - 30
        os.utime(os.path.join(self.products, "a.knxprod"), (t, t))       # touched, same bytes
        s = self.sync()
        self.assertEqual((s["changed"], s["unchanged"]), ([], 1))
        self.assertEqual(load_json(os.path.join(self.data, "index.json"))["generated"],
                         generated)

    def test_files_are_never_modified(self):
        self.put("a.knxprod", full_knxprod())
        path = os.path.join(self.products, "a.knxprod")
        before = (K._sha256_file(path), os.stat(path).st_mtime_ns)
        self.sync()
        self.sync()
        self.assertEqual((K._sha256_file(path), os.stat(path).st_mtime_ns), before)
        self.assertEqual(sorted(os.listdir(self.products)), ["a.knxprod"])

    def test_subfolders_and_ignored_names(self):
        self.put("vendor/2017/b.knxprod", full_knxprod())
        self.put(".hidden.zip", full_knxprod())
        self.put(".cache/c.knxprod", full_knxprod())
        self.put("notes.txt", b"hello")
        self.put("d.zip.part", full_knxprod())
        s = self.sync()
        self.assertEqual(s["added"], ["vendor/2017/b.knxprod"])
        self.assertEqual(self.index()[APP2]["sources"], ["vendor/2017/b.knxprod"])

    def test_outer_zip_with_knxprod_inside(self):
        with open(make_knxprod(self.tmp), "rb") as f:
            self.put("Synthetic.knxprod.zip", f.read())
        s = self.sync()
        self.assertEqual(s["apps_total"], 2)

    def test_delete_drops_only_orphans(self):
        self.put("a.knxprod", full_knxprod())
        self.put("b.knxprod", zip_bytes(members("master", "hardware", "app2")))
        self.sync()
        self.assertEqual(self.index()[APP2]["sources"], ["a.knxprod", "b.knxprod"])
        os.remove(os.path.join(self.products, "a.knxprod"))
        s = self.sync()
        self.assertEqual((s["removed"], s["apps_removed"]), (["a.knxprod"], [APP1]))
        self.assertEqual(sorted(self.index()), [APP2])
        self.assertEqual(self.index()[APP2]["sources"], ["b.knxprod"])
        self.assertFalse(os.path.exists(os.path.join(self.data, "apps", APP1 + ".json")))
        self.assertTrue(os.path.exists(os.path.join(self.data, "apps", APP2 + ".json")))
        self.assertEqual(sorted(self.files_state()), ["b.knxprod"])

    def test_change_replaces_content(self):
        self.put("a.knxprod", full_knxprod())
        self.sync()
        self.put("a.knxprod", zip_bytes(members("master", "hardware", "app1")))
        s = self.sync()
        self.assertEqual((s["changed"], s["apps_removed"]), (["a.knxprod"], [APP2]))
        self.assertEqual(sorted(self.index()), [APP1])
        self.assertEqual(self.files_state()["a.knxprod"]["apps"], [APP1])

    def test_record_is_rebuilt_from_the_remaining_file(self):
        self.put("a.knxprod", full_knxprod())
        self.put("b.knxprod", zip_bytes(members("master", "hardware", "app2",
                                                app2_name="Test actuator 00AB (b)")))
        self.sync()
        name = lambda: load_json(os.path.join(self.data, "apps", APP2 + ".json"))["name"]
        self.assertEqual(name(), "Test actuator 00AB (b)")             # b was read last
        os.remove(os.path.join(self.products, "b.knxprod"))
        s = self.sync()
        self.assertEqual(s["removed"], ["b.knxprod"])
        self.assertEqual(name(), "Test actuator 00AB")                  # rebuilt from a
        self.assertEqual(s["refreshed"], ["a.knxprod"])
        self.assertEqual(self.index()[APP2]["written_by"], "a.knxprod")

    def test_broken_file_and_recovery(self):
        self.put("broken.zip", b"PK\x03\x04 truncated")
        s = self.sync()
        self.assertIn("broken.zip", s["errors"])
        self.assertEqual(self.files_state()["broken.zip"]["status"], "error")
        s = self.sync()                                                 # not retried
        self.assertEqual((s["errors"], s["unchanged"]), ({}, 1))
        self.put("broken.zip", full_knxprod())                          # fixed by the user
        s = self.sync()
        self.assertEqual((s["changed"], s["apps_total"]), (["broken.zip"], 2))

    def test_settle_time(self):
        self.put("fresh.knxprod", full_knxprod(), age=0)
        s = self.sync(settle=60)
        self.assertEqual((s["pending"], s["apps_total"]), (["fresh.knxprod"], 0))
        s = self.sync(settle=0)
        self.assertEqual(s["added"], ["fresh.knxprod"])

    def test_missing_directory_keeps_the_index(self):
        self.put("a.knxprod", full_knxprod())
        self.sync()
        away = self.products + ".away"
        os.rename(self.products, away)
        s = self.sync()
        self.assertEqual((s["missing"], s["removed"], s["apps_total"]), (True, [], 2))
        os.rename(away, self.products)
        s = self.sync()
        self.assertEqual((s["missing"], s["unchanged"], s["apps_total"]), (False, 1, 2))
        os.remove(os.path.join(self.products, "a.knxprod"))      # an empty folder does count
        s = self.sync()
        self.assertEqual((s["removed"], s["apps_total"]), (["a.knxprod"], 0))

    def test_lost_state_is_reconciled(self):
        self.put("a.knxprod", full_knxprod())
        self.put("b.knxprod", zip_bytes(members("master", "hardware", "app2")))
        self.sync()
        os.remove(os.path.join(self.data, "files.json"))
        os.remove(os.path.join(self.products, "a.knxprod"))
        s = self.sync()
        self.assertEqual(s["removed"], ["a.knxprod"])
        self.assertEqual(sorted(self.index()), [APP2])

    def test_index_of_an_older_format_is_rebuilt(self):
        self.put("a.knxprod", full_knxprod())
        self.sync()
        p = K._paths(self.data)
        for key in ("index", "files"):
            d = load_json(p[key])
            d["schema"] = K.SCHEMA_VERSION - 1
            K.atomic_write_json(p[key], d)
        old = load_json(os.path.join(p["apps"], APP1 + ".json"))
        old.update(schema=K.SCHEMA_VERSION - 1, parameters=[])
        K.atomic_write_json(os.path.join(p["apps"], APP1 + ".json"), old)
        lines = []
        s = self.sync(log=lines.append)
        self.assertEqual(s["rebuilt_from_schema"], K.SCHEMA_VERSION - 1)
        self.assertEqual((s["added"], s["unchanged"], s["apps_total"]), (["a.knxprod"], 0, 2))
        self.assertTrue(any("rebuilding the index" in line for line in lines), lines)
        self.assertEqual(load_json(p["index"])["schema"], K.SCHEMA_VERSION)
        self.assertEqual(load_json(p["files"])["schema"], K.SCHEMA_VERSION)
        rec = load_json(os.path.join(p["apps"], APP1 + ".json"))
        self.assertEqual((rec["schema"], len(rec["parameters"])), (K.SCHEMA_VERSION, 11))
        s = self.sync()
        self.assertNotIn("rebuilt_from_schema", s)
        self.assertEqual(s["unchanged"], 1)

    def test_server_start_rebuilds_an_older_index(self):
        self.put("a.knxprod", full_knxprod())
        self.sync()
        p = K._paths(self.data)
        for key in ("index", "files"):                     # as an older version left it
            d = load_json(p[key])
            d.pop("schema")
            K.atomic_write_json(p[key], d)
        srv = Server(self.products, self.data)
        self.addCleanup(srv.stop)
        self.assertTrue(any("rebuilding the index" in line for line in srv.lines), srv.lines)
        self.assertEqual(srv.health()["apps"], 2)
        res, o, _ = srv.tool("prod-parameters", {"app_id": APP1, "address": "0x4A64"})
        self.assertEqual(sorted(x["id"] for x in o["parameters"]), ["UP-10", "UP-11"])
        self.assertEqual(load_json(p["files"])["schema"], K.SCHEMA_VERSION)

    def test_upload_naming(self):
        os.makedirs(self.data, exist_ok=True)

        def upload(name, data):
            tmp = os.path.join(self.products, ".upload-test.tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            sha = K._sha256_file(tmp)
            return K.store_upload(self.products, self.data, tmp, K._upload_name(name), sha)

        r = upload("Test.knxprod", full_knxprod())
        self.assertEqual((r["file"], r["stored"]), ("Test.knxprod", True))
        r = upload("Test.knxprod", zip_bytes(members("master", "hardware", "app2")))
        self.assertEqual((r["file"], r["renamed_from"]), ("Test-2.knxprod", "Test.knxprod"))
        r = upload("Other.knxprod", full_knxprod())                     # same bytes as Test
        self.assertEqual((r["stored"], r["duplicate_of"]), (False, "Test.knxprod"))
        self.assertEqual(sorted(os.listdir(self.products)), ["Test-2.knxprod", "Test.knxprod"])
        s = self.sync()                                                  # nothing left to do
        self.assertEqual((s["added"], s["changed"], s["unchanged"]), ([], [], 2))
        self.assertEqual(K._upload_name("../../etc/passwd"), "passwd.zip")
        self.assertEqual(K._upload_name(".hidden.knxprod"), "hidden.knxprod")
        self.assertTrue(K._upload_name("").startswith("upload-"))


# ---------------------------------------------------------------------------
# 8. HTTP: guards, MCP, upload, scanner
# ---------------------------------------------------------------------------

class TestHttp(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = _tmpdir()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.products = os.path.join(cls.tmp, "knxprod")
        cls.data = os.path.join(cls.tmp, "index")
        write_file(os.path.join(cls.products, "start.knxprod"),
                   zip_bytes(members("master", "hardware", "app1")))
        cls.srv = Server(cls.products, cls.data, scan_interval=0.2)
        cls.addClassCleanup(cls.srv.stop)

    def test_a_initial_sync_before_serving(self):
        h = self.srv.health()
        self.assertEqual((h["ok"], h["apps"], h["files"], h["file_errors"]), (True, 1, 1, 0))
        self.assertEqual(h["mdns"], {"enabled": False, "name": None, "address": None,
                                     "status": "off"})
        self.assertIs(h["skill"], True)
        self.assertTrue(any("indexed start.knxprod" in ln for ln in self.srv.lines))

    def test_b_host_guard(self):
        for host in ("evil.example", "127.0.0.1.nip.io:%d" % self.srv.port, "", "myserver.lan"):
            status, _, _ = self.srv.request("GET", "/healthz", host=host)
            self.assertEqual(status, 403, host)
        for host in ("127.0.0.1", "localhost:%d" % self.srv.port, "knx-box.local", "[::1]:8095",
                     "192.168.10.20:8095"):
            status, _, _ = self.srv.request("GET", "/healthz", host=host)
            self.assertEqual(status, 200, host)
        other = Server(self.products, os.path.join(self.tmp, "index-2"),
                       allowed_hosts=["myserver.lan"])
        try:
            status, _, _ = other.request("GET", "/healthz", host="myserver.lan:8095")
            self.assertEqual(status, 200)
        finally:
            other.stop()

    def test_c_mcp_over_http(self):
        s = self.srv
        status, h, body = s.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "0"}}})
        self.assertEqual(status, 200)
        self.assertTrue(h["Content-Type"].startswith("application/json"))
        self.assertIsNone(h.get("Mcp-Session-Id"))
        self.assertEqual(json.loads(body)["result"]["protocolVersion"], "2025-03-26")
        status, _, body = s.mcp({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, b""))
        status, _, body = s.mcp({"jsonrpc": "2.0", "id": 2, "method": "no/such"})
        self.assertEqual(json.loads(body)["error"]["code"], -32601)
        status, _, body = s.request("POST", "/mcp", b"{not json",
                                    {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)["error"]["code"]), (400, -32700))
        status, _, body = s.mcp([{"jsonrpc": "2.0", "id": 3, "method": "ping"},
                                 {"jsonrpc": "2.0", "method": "notifications/x"}])
        self.assertEqual([m["id"] for m in json.loads(body)], [3])
        self.assertEqual(s.request("GET", "/mcp")[0], 405)
        self.assertEqual(s.mcp({"jsonrpc": "2.0", "id": 4, "method": "ping"},
                               {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(s.mcp({"jsonrpc": "2.0", "id": 5, "method": "ping"},
                               {"Origin": "http://192.168.1.5:8080"})[0], 200)
        status, _, body = s.mcp({"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
        self.assertEqual(sum(1 for t in json.loads(body)["result"]["tools"]
                             if t["name"].startswith("prod-")), 5)
        res, o, size = s.tool("prod-comobjects", {"app_id": APP1, "numbers": [1]})
        self.assertFalse(res["isError"])
        self.assertLessEqual(size, K.RESPONSE_BUDGET)
        self.assertEqual(len(o["comobjects"][0]["variants"]), 2)

    def test_d_upload_cors(self):
        s = self.srv
        status, h, _ = s.request("OPTIONS", "/upload",
                                 headers={"Origin": "http://192.168.1.57",
                                          "Access-Control-Request-Method": "POST",
                                          "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(status, 204)
        self.assertEqual(h["Access-Control-Allow-Origin"], "http://192.168.1.57")
        self.assertEqual(h["Access-Control-Allow-Private-Network"], "true")
        status, h, _ = s.request("OPTIONS", "/upload", headers={"Origin": "http://knx-stick.local"})
        self.assertEqual(status, 204)
        for bad in ("http://evil.example", "http://8.8.8.8", "https://192.168.1.57.nip.io"):
            status, h, _ = s.request("OPTIONS", "/upload", headers={"Origin": bad})
            self.assertEqual(status, 403, bad)
            self.assertIsNone(h.get("Access-Control-Allow-Origin"))
        self.assertEqual(s.request("OPTIONS", "/mcp", headers={"Origin": "http://192.168.1.57"})[0],
                         405)
        status, _, _ = s.request("POST", "/upload?name=x.knxprod", full_knxprod(),
                                 {"Content-Type": "application/zip", "Origin": "http://8.8.8.8"})
        self.assertEqual(status, 403)

    def test_e_upload_stores_and_indexes(self):
        s = self.srv
        before = sorted(os.listdir(self.products))
        status, _, _ = s.request("POST", "/upload?name=x.zip", b"hello",
                                 {"Content-Type": "application/zip"})
        self.assertEqual(status, 415)
        evil = zip_bytes([("../M-FFF0_A-1234-56-ABCD.xml", b"<KNX/>")])
        status, _, body = s.request("POST", "/upload?name=evil.zip", evil,
                                    {"Content-Type": "application/zip"})
        self.assertEqual(status, 422)
        self.assertIn("unsafe member name", json.loads(body)["error"])
        self.assertEqual(sorted(os.listdir(self.products)), before)
        status, h, body = s.request("POST", "/upload?name=../../Vendor%20Products.knxprod",
                                    full_knxprod(),
                                    {"Content-Type": "application/zip",
                                     "Origin": "http://192.168.1.57"})
        self.assertEqual(status, 200, body)
        self.assertEqual(h["Access-Control-Allow-Origin"], "http://192.168.1.57")
        o = json.loads(body)
        self.assertEqual((o["ok"], o["file"], o["stored"]), (True, "Vendor_Products.knxprod", True))
        self.assertEqual(o["apps_added"], [APP2, APP1])
        self.assertEqual(o["apps_new"], [APP2])
        self.assertEqual(o["products"], 3)
        self.assertTrue(os.path.isfile(os.path.join(self.products, "Vendor_Products.knxprod")))
        status, _, body = s.request("POST", "/upload?name=again.knxprod", full_knxprod(),
                                    {"Content-Type": "application/octet-stream"})
        o = json.loads(body)
        self.assertEqual((status, o["stored"], o["duplicate_of"]),
                         (200, False, "Vendor_Products.knxprod"))
        self.assertFalse(os.path.exists(os.path.join(self.products, "again.knxprod")))
        self.assertEqual([f for f in os.listdir(self.products) if f.startswith(".")], [])
        time.sleep(1.0)                      # several scan intervals: nothing to re-read
        self.assertEqual(sum("indexed Vendor_Products.knxprod" in ln for ln in s.lines), 0)
        self.assertEqual(s.health()["apps"], 2)

    def test_f_scanner_follows_the_directory(self):
        s = self.srv
        write_file(os.path.join(self.products, "later", "extra.knxprod"),
                   zip_bytes(members("master", "hardware", "app2")))
        self.assertTrue(wait_for(lambda: any("indexed later/extra.knxprod" in ln
                                             for ln in s.lines)))
        os.remove(os.path.join(self.products, "Vendor_Products.knxprod"))
        os.remove(os.path.join(self.products, "start.knxprod"))
        self.assertTrue(wait_for(lambda: s.health()["apps"] == 1))
        res, o, _ = s.tool("prod-applications", {})
        self.assertEqual([a["app_id"] for a in o["applications"]], [APP2])

    def test_g_upload_size_limit(self):
        sock = socket.create_connection(("127.0.0.1", self.srv.port), timeout=30)
        try:
            sock.sendall(("POST /upload?name=big.zip HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                          "Content-Type: application/zip\r\nContent-Length: %d\r\n\r\n"
                          % (K.MAX_UPLOAD + 1)).encode())
            reply = sock.recv(4096).decode("latin-1")
        finally:
            sock.close()
        self.assertTrue(reply.startswith("HTTP/1.1 413"), reply[:80])

    def test_h_one_log_line_per_request(self):
        time.sleep(0.3)
        before = len(self.srv.lines)
        self.srv.request("GET", "/healthz")
        self.srv.mcp({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        time.sleep(0.05)
        new = [ln for ln in self.srv.lines[before:] if " /healthz " in ln or " /mcp " in ln]
        self.assertEqual(len(new), 2, new)
        self.assertIn("GET /healthz 200", new[0])
        self.assertIn("POST /mcp 200", new[1])
        self.assertIn("tools/list", new[1])

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_i_read_only_product_directory(self):
        ro = os.path.join(self.tmp, "ro")
        os.makedirs(ro)
        write_file(os.path.join(ro, "a.knxprod"), full_knxprod())
        os.chmod(ro, 0o555)
        srv = Server(ro, os.path.join(self.tmp, "index-ro"))
        try:
            self.assertEqual(srv.health()["apps"], 2)
            status, _, body = srv.request("POST", "/upload?name=b.knxprod", full_knxprod(),
                                          {"Content-Type": "application/zip"})
            self.assertEqual(status, 503, body)
        finally:
            srv.stop()
            os.chmod(ro, 0o755)


# ---------------------------------------------------------------------------
# 9. mDNS / DNS-SD without network
# ---------------------------------------------------------------------------

SVC = ("_knxprod-mcp", "_tcp", "local")
INST = ("knxprod-mcp on testhost",) + SVC
HOST = ("testhost", "local")


def core():
    return K.MdnsCore("knxprod-mcp on testhost", "testhost", 8095, ["192.168.1.10"],
                      ["txtvers=1", "path=/mcp", "v=" + K.VERSION])


def records(msg, section):
    return [(r["name"], r["type"], r["class"], r["ttl"], K.record_value(msg, r))
            for r in msg["records"] if r["section"] == section]


def txt_strings(blob):
    out, i = [], 0
    while i < len(blob):
        n = blob[i]
        out.append(blob[i + 1:i + 1 + n].decode())
        i += 1 + n
    return out


class FakeSock:
    """Stands in for the UDP socket: records what is sent, plays back replies."""

    def __init__(self, replies=()):
        self.sent, self.replies, self.timeout = [], list(replies), 0.01

    def sendto(self, data, dest):
        self.sent.append((data, dest))

    def settimeout(self, t):
        self.timeout = t

    def recvfrom(self, n):
        if self.replies:
            return self.replies.pop(0)
        time.sleep(min(self.timeout, 0.25))
        raise socket.timeout()

    def close(self):
        pass


class TestMdnsWire(unittest.TestCase):

    def test_names(self):
        data = K.encode_name(INST)
        self.assertEqual(K.read_name(data, 0), (INST, len(data)))
        with self.assertRaises(ValueError):
            K.encode_name(("x" * 64, "local"))
        with self.assertRaises(ValueError):
            K.encode_name(("", "local"))
        self.assertEqual(len(K.clip_label("ä" * 40).encode()), 62)

    def test_compression_pointers_in_queries(self):
        head = struct.pack("!6H", 0x1234, 0, 3, 0, 0, 0)
        q1 = K.encode_name(SVC) + struct.pack("!2H", K.T_PTR, K.C_IN)
        # "x" + pointer to the service name; "y" + pointer into the middle ("_tcp.local")
        q2 = b"\x01x" + bytes([0xC0, 12]) + struct.pack("!2H", K.T_SRV, K.C_IN)
        q3 = b"\x01y" + bytes([0xC0, 12 + 1 + len("_knxprod-mcp")]) + struct.pack(
            "!2H", K.T_A, K.C_IN)
        msg = K.parse_message(head + q1 + q2 + q3)
        self.assertEqual(msg["id"], 0x1234)
        self.assertEqual([q[0] for q in msg["questions"]],
                         [SVC, ("x",) + SVC, ("y", "_tcp", "local")])

    def test_bad_pointers_are_rejected(self):
        head = struct.pack("!6H", 0, 0, 1, 0, 0, 0)
        for name in (bytes([0xC0, 12]),                  # points at itself
                     bytes([0xC0, 40]),                  # points forward
                     bytes([0xC0]),                      # truncated
                     b"\x05abc"):                        # label runs past the end
            with self.assertRaises(ValueError, msg=name):
                K.parse_message(head + name + struct.pack("!2H", 1, 1))

    def test_next_instance_name(self):
        self.assertEqual(K.next_instance_name("knxprod-mcp on a"), "knxprod-mcp on a (2)")
        self.assertEqual(K.next_instance_name("knxprod-mcp on a (2)"), "knxprod-mcp on a (3)")
        long = K.next_instance_name("x" * 63)
        self.assertTrue(long.endswith(" (2)"))
        self.assertLessEqual(len(long.encode()), 63)


class TestMdnsAnswers(unittest.TestCase):

    def ask(self, questions, src=("192.168.1.20", 5353), mid=0, known=()):
        q = K.parse_message(K.build_message(mid, 0, questions=questions, answers=known))
        return [(K.parse_message(pkt), dest) for pkt, dest in core().respond(q, src)]

    def test_ptr_query_answers_with_additionals(self):
        [(msg, dest)] = self.ask([(SVC, K.T_PTR, K.C_IN)])
        self.assertEqual(dest, ("224.0.0.251", 5353))
        self.assertEqual((msg["id"], msg["flags"]), (0, 0x8400))
        self.assertEqual(records(msg, "an"), [(SVC, K.T_PTR, 1, 4500, K._key(INST))])
        add = {r[1]: r for r in records(msg, "ar")}
        self.assertEqual(sorted(add), [K.T_A, K.T_TXT, K.T_SRV])
        self.assertEqual(add[K.T_SRV], (INST, K.T_SRV, 0x8001, 120, (0, 0, 8095, K._key(HOST))))
        self.assertEqual(add[K.T_A][2:], (0x8001, 120, socket.inet_aton("192.168.1.10")))
        self.assertEqual(add[K.T_TXT][:4], (INST, K.T_TXT, 0x8001, 4500))
        self.assertEqual(txt_strings(add[K.T_TXT][4]), ["txtvers=1", "path=/mcp",
                                                        "v=" + K.VERSION])

    def test_service_enumeration(self):
        [(msg, _)] = self.ask([(("_services", "_dns-sd", "_udp", "local"), K.T_PTR, K.C_IN)])
        self.assertEqual(records(msg, "an")[0][4], K._key(SVC))
        self.assertEqual(records(msg, "ar"), [])

    def test_srv_txt_a_any(self):
        [(msg, _)] = self.ask([(INST, K.T_SRV, K.C_IN)])
        self.assertEqual([r[1] for r in records(msg, "an")], [K.T_SRV])
        self.assertEqual([r[1] for r in records(msg, "ar")], [K.T_A])
        [(msg, _)] = self.ask([(INST, K.T_TXT, K.C_IN)])
        self.assertEqual([r[1] for r in records(msg, "an")], [K.T_TXT])
        [(msg, _)] = self.ask([(HOST, K.T_A, K.C_IN)])
        self.assertEqual(records(msg, "an")[0][4], socket.inet_aton("192.168.1.10"))
        [(msg, _)] = self.ask([(INST, K.T_ANY, K.C_IN)])
        self.assertEqual(sorted(r[1] for r in records(msg, "an")), [K.T_TXT, K.T_SRV])
        upper = tuple(lb.upper() for lb in INST)                  # names are case-insensitive
        self.assertEqual(len(self.ask([(upper, K.T_SRV, K.C_IN)])), 1)
        self.assertEqual(self.ask([(("other",) + SVC, K.T_SRV, K.C_IN)]), [])
        self.assertEqual(self.ask([(HOST, K.T_AAAA, K.C_IN)]), [])

    def test_qu_bit_and_legacy_unicast(self):
        [(msg, dest)] = self.ask([(SVC, K.T_PTR, K.C_IN | K.QU_BIT)])
        self.assertEqual(dest, ("192.168.1.20", 5353))            # unicast to the querier
        [(msg, dest)] = self.ask([(SVC, K.T_PTR, K.C_IN)], src=("192.168.1.20", 40000),
                                 mid=0x4242)
        self.assertEqual(dest, ("192.168.1.20", 40000))
        self.assertEqual(msg["id"], 0x4242)
        self.assertEqual(msg["questions"], [(SVC, K.T_PTR, K.C_IN)])
        for r in msg["records"]:
            self.assertLessEqual(r["ttl"], 10)
            self.assertEqual(r["class"], 1)                       # no cache-flush bit

    def test_known_answer_suppression_and_responses(self):
        ptr = core().ptr
        self.assertEqual(self.ask([(SVC, K.T_PTR, K.C_IN)], known=[ptr.wire()]), [])
        self.assertEqual(len(self.ask([(SVC, K.T_PTR, K.C_IN)], known=[ptr.wire(ttl=100)])), 1)
        reply = K.parse_message(core().announcement())
        self.assertEqual(core().respond(reply, ("192.168.1.20", 5353)), [])

    def test_announcement_goodbye_probe(self):
        msg = K.parse_message(core().announcement())
        self.assertEqual(sorted(r[1] for r in records(msg, "an")),
                         [K.T_A, K.T_PTR, K.T_PTR, K.T_TXT, K.T_SRV])
        bye = K.parse_message(core().announcement(ttl=0))
        self.assertEqual({r["ttl"] for r in bye["records"]}, {0})
        probe = K.parse_message(core().probe())
        self.assertEqual((probe["flags"], probe["questions"]), (0, [(INST, K.T_ANY, K.C_IN)]))
        self.assertEqual(sorted(r[1] for r in records(probe, "ns")), [K.T_TXT, K.T_SRV])

    def test_conflicts(self):
        mine = core()
        other = K.MdnsCore(mine.instance, "otherhost", 8095, ["192.168.1.11"], mine.txt)
        self.assertTrue(mine.conflicts(K.parse_message(other.announcement())))
        self.assertFalse(mine.conflicts(K.parse_message(mine.announcement())))
        self.assertFalse(mine.conflicts(K.parse_message(other.announcement(ttl=0))))
        self.assertFalse(mine.conflicts(K.parse_message(other.probe())))


class TestMdnsResponder(unittest.TestCase):
    PLAN = {"addresses": ["192.168.1.10"], "interface": "192.168.1.10",
            "netmask": "255.255.255.0"}

    def test_probe_conflict_gets_a_suffix(self):
        mine = core()
        other = K.MdnsCore(mine.instance, "otherhost", 8095, ["192.168.1.11"], mine.txt)
        sock = FakeSock([(other.announcement(), ("192.168.1.11", 5353))])
        lines = []
        r = K.MdnsResponder(mine, self.PLAN, lines.append, sock=sock)
        self.assertEqual(r.probe(), "knxprod-mcp on testhost (2)")
        asked = [K.parse_message(pkt)["questions"][0][0][0] for pkt, _ in sock.sent]
        self.assertEqual(asked[0], "knxprod-mcp on testhost")
        self.assertEqual(asked[-3:], ["knxprod-mcp on testhost (2)"] * 3)
        self.assertTrue(any("is taken" in ln for ln in lines))

    def test_announce_answer_goodbye(self):
        mine = core()
        query = K.build_message(0x7777, 0, questions=[(SVC, K.T_PTR, K.C_IN)])
        sock = FakeSock()
        r = K.MdnsResponder(mine, self.PLAN, lambda line: None, sock=sock)
        r.start()
        self.assertTrue(wait_for(lambda: r.status == "announced", 5))
        sock.replies.append((query, ("192.168.1.20", 41000)))       # legacy unicast query
        sock.replies.append((query, ("10.9.9.9", 41000)))           # off-link: ignored
        self.assertTrue(wait_for(lambda: any(d == ("192.168.1.20", 41000)
                                             for _, d in sock.sent), 5))
        time.sleep(0.6)                                             # give it time to (not) answer
        self.assertFalse(any(d[0] == "10.9.9.9" for _, d in sock.sent))
        info = r.info()
        self.assertEqual((info["enabled"], info["name"], info["address"]),
                         (True, "knxprod-mcp on testhost", "192.168.1.10"))
        r.stop()
        last = K.parse_message(sock.sent[-1][0])
        self.assertEqual((sock.sent[-1][1], {x["ttl"] for x in last["records"]}),
                         (("224.0.0.251", 5353), {0}))
        self.assertEqual(r.info()["enabled"], False)


def iface(name, addr, flags=0x1043, mask="255.255.255.0"):
    return {"name": name, "address": addr, "netmask": mask, "flags": flags}


LO = iface("lo", "127.0.0.1", 0x49, "255.0.0.0")


class TestMdnsAddress(unittest.TestCase):

    def plan(self, bind, interfaces, container=False, default_route="", override=None):
        return K.mdns_plan(bind, override, interfaces, default_route, container)

    def test_specific_and_any_bind(self):
        host = [LO, iface("eth0", "192.168.1.10"), iface("wg0", "10.0.0.2", 0xd1),
                iface("docker0", "172.17.0.1", mask="255.255.0.0"), iface("tap0", "192.168.45.1"),
                iface("eth0:1", "192.168.1.11")]
        p = self.plan("192.168.1.10", host)
        self.assertEqual((p["addresses"], p["netmask"]), (["192.168.1.10"], "255.255.255.0"))
        p = self.plan("0.0.0.0", host, default_route="192.168.1.10")
        self.assertEqual(p["addresses"], ["192.168.1.10", "192.168.1.11"])
        p = self.plan("0.0.0.0", host, default_route="192.168.45.1")
        self.assertEqual(p["addresses"], ["192.168.45.1"])
        p = self.plan("::", host, default_route="192.168.1.10")
        self.assertEqual(p["addresses"], ["192.168.1.10", "192.168.1.11"])

    def test_nothing_to_announce(self):
        p = self.plan("127.0.0.1", [LO])
        self.assertEqual((p["addresses"], p["warning"]), ([], False))
        p = self.plan("0.0.0.0", [LO, iface("wg0", "10.0.0.2", 0xd1)])
        self.assertEqual(p["addresses"], [])
        self.assertIn("no multicast-capable", p["reason"])
        p = self.plan("fd00::5", [LO])
        self.assertIn("IPv6", p["reason"])
        p = self.plan("172.17.0.1", [LO, iface("docker0", "172.17.0.1", mask="255.255.0.0")])
        self.assertIn("bridge", p["reason"])

    def test_container_bridge_only_is_skipped_with_warning(self):
        bridged = [LO, iface("eth0", "172.18.0.2", mask="255.255.0.0")]
        for bind in ("0.0.0.0", "172.18.0.2"):
            p = self.plan(bind, bridged, container=True)
            self.assertEqual((p["addresses"], p["warning"]), ([], True))
            self.assertIn("host networking", p["reason"])

    def test_container_with_host_networking_or_lan_address(self):
        hostnet = [LO, iface("eth0", "192.168.1.10"), iface("docker0", "172.17.0.1"),
                   iface("vethab12", "169.254.3.3")]
        p = self.plan("0.0.0.0", hostnet, container=True, default_route="192.168.1.10")
        self.assertEqual(p["addresses"], ["192.168.1.10"])
        macvlan = [LO, iface("eth0", "192.168.1.50")]
        self.assertEqual(self.plan("0.0.0.0", macvlan, container=True)["addresses"],
                         ["192.168.1.50"])

    def test_override(self):
        p = self.plan("0.0.0.0", [LO], override=["192.168.5.5"])
        self.assertEqual(p["addresses"], ["192.168.5.5"])
        p = self.plan("0.0.0.0", [LO], override=["127.0.0.1"])
        self.assertEqual(p["addresses"], [])


class TestMdnsService(unittest.TestCase):

    def setUp(self):
        self.tmp = _tmpdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.saved = (K.ipv4_interfaces, K.in_container, K.default_route_ipv4)
        self.addCleanup(self.restore)

    def restore(self):
        K.ipv4_interfaces, K.in_container, K.default_route_ipv4 = self.saved

    def test_bridge_only_container_skips_with_warning(self):
        K.ipv4_interfaces = lambda: [LO, iface("eth0", "172.18.0.2", mask="255.255.0.0")]
        K.in_container = lambda: True
        K.default_route_ipv4 = lambda: "172.18.0.2"
        srv = Server(os.path.join(self.tmp, "p"), os.path.join(self.tmp, "d"),
                     bind="127.0.0.1", mdns={"enabled": True})
        self.addCleanup(srv.stop)
        self.assertIsNone(srv.svc.mdns)
        # the server itself listens on 127.0.0.1 here: nothing to announce, quietly
        self.assertEqual(srv.health()["mdns"]["status"], "skipped")
        plan = K.mdns_plan("0.0.0.0")
        self.assertIn("host networking", plan["reason"])
        svc_lines = []
        svc = K.Service({"products": os.path.join(self.tmp, "p"),
                         "data": os.path.join(self.tmp, "d2"), "bind": "127.0.0.1", "port": 0,
                         "mdns": {"enabled": True}}, log=svc_lines.append)
        self.addCleanup(svc.close)
        svc.httpd.server_address = ("0.0.0.0", svc.port)          # as if bound to all
        self.assertIsNone(svc.start_mdns())
        self.assertTrue(any("host networking" in ln for ln in svc_lines))
        self.assertEqual(svc.httpd.mdns_info()["status"], "skipped")

    def test_bind_auto(self):
        K.ipv4_interfaces = lambda: [LO]
        with self.assertRaises(K.ConfigError):
            K.make_server("auto", 0, self.tmp, self.tmp)
        orig = K.mdns_plan
        K.mdns_plan = lambda bind, *a, **k: {"addresses": ["127.0.0.1"], "interface": "127.0.0.1"}
        try:
            httpd = K.make_server("auto", 0, self.tmp, self.tmp)
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
            httpd.server_close()
        finally:
            K.mdns_plan = orig

    def test_config_and_health_command(self):
        with env(KNXPROD_MDNS="0"):
            self.assertFalse(K.resolve_config(K.build_parser().parse_args(["serve"]))["mdns"]
                             ["enabled"])
        with env(KNXPROD_MDNS_NAME="KNX office", KNXPROD_MDNS_HOSTNAME="Box.local.",
                 KNXPROD_MDNS_ADDRESS="192.168.1.10, 192.168.1.11"):
            c = K.resolve_config(K.build_parser().parse_args(["serve"]))
            self.assertEqual(c["mdns"], {"enabled": True, "name": "KNX office", "hostname": "Box",
                                         "address": ["192.168.1.10", "192.168.1.11"]})
            c = K.resolve_config(K.build_parser().parse_args(["serve", "--no-mdns"]))
            self.assertFalse(c["mdns"]["enabled"])
        for bad in ({"KNXPROD_MDNS_ADDRESS": "fe80::1"}, {"KNXPROD_MDNS_HOSTNAME": "a.b"},
                    {"KNXPROD_MDNS_NAME": "x" * 64}):
            with env(**bad):
                with self.assertRaises(K.ConfigError, msg=str(bad)):
                    K.resolve_config(K.build_parser().parse_args(["serve"]))
        data = os.path.join(self.tmp, "d")
        with open(os.devnull, "w") as dn, contextlib.redirect_stderr(dn):
            self.assertEqual(K.health_check(data), 1)            # nothing running
        srv = Server(os.path.join(self.tmp, "p"), data)
        self.addCleanup(srv.stop)
        K.atomic_write_json(os.path.join(data, "server.json"), {"host": "0.0.0.0",
                                                               "port": srv.port})
        self.assertEqual(K.health_check(data), 0)
        srv.stop()
        with open(os.devnull, "w") as dn, contextlib.redirect_stderr(dn):
            self.assertEqual(K.health_check(data, timeout=1), 1)


# ---------------------------------------------------------------------------
# 10. optional: live mDNS on a LAN address (KNXPROD_TEST_MDNS_ADDRESS)
# ---------------------------------------------------------------------------

LIVE = os.environ.get("KNXPROD_TEST_MDNS_ADDRESS")
LIVE_PORT = int(os.environ.get("KNXPROD_TEST_MDNS_PORT") or 0)


def mdns_socket(addr, port=0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if port:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    if port:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton("224.0.0.251") + socket.inet_aton(addr))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    return s


def collect(sock, seconds, want):
    """Messages received within `seconds` for which want(msg) is true."""
    out, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        sock.settimeout(max(0.05, end - time.monotonic()))
        try:
            data, src = sock.recvfrom(9000)
        except socket.timeout:
            break
        try:
            msg = K.parse_message(data)
        except ValueError:
            continue
        if want(msg):
            out.append((msg, src))
    return out


@unittest.skipUnless(LIVE, "set KNXPROD_TEST_MDNS_ADDRESS to a LAN address of this host")
class TestMdnsLive(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = _tmpdir()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        write_file(os.path.join(cls.tmp, "p", "a.knxprod"), full_knxprod())
        cls.name = "knxprod-mcp live test %d" % os.getpid()
        cls.srv = Server(os.path.join(cls.tmp, "p"), os.path.join(cls.tmp, "d"), bind=LIVE,
                         port=LIVE_PORT, mdns={"enabled": True, "name": cls.name})
        cls.addClassCleanup(cls.srv.stop)
        assert wait_for(lambda: cls.srv.health()["mdns"]["status"] == "announced", 10)
        cls.inst = K._key((cls.name,) + SVC)

    def mine(self, msg):
        return any(r["type"] == K.T_PTR and K.record_value(msg, r) == self.inst
                   for r in msg["records"])

    def check_additionals(self, msg):
        add = {r["type"]: K.record_value(msg, r) for r in msg["records"]}
        self.assertEqual(add[K.T_SRV][2], self.srv.port)
        self.assertEqual(add[K.T_A], socket.inet_aton(LIVE))
        self.assertIn(b"path=/mcp", add[K.T_TXT])
        return add

    def test_a_legacy_unicast_browse(self):
        s = mdns_socket(LIVE)
        try:
            s.sendto(K.build_message(0x5151, 0, questions=[(SVC, K.T_PTR, K.C_IN)]),
                     ("224.0.0.251", 5353))
            got = collect(s, 2, self.mine)
        finally:
            s.close()
        self.assertTrue(got, "no unicast answer")
        msg, src = got[0]
        self.assertEqual((msg["id"], msg["questions"]), (0x5151, [(SVC, K.T_PTR, K.C_IN)]))
        self.check_additionals(msg)

    def test_b_multicast_browse_and_connect(self):
        s = mdns_socket(LIVE, 5353)
        try:
            s.sendto(K.build_message(0, 0, questions=[(SVC, K.T_PTR, K.C_IN)]),
                     ("224.0.0.251", 5353))
            got = collect(s, 2, lambda m: m["flags"] & 0x8000 and self.mine(m))
        finally:
            s.close()
        self.assertTrue(got, "no multicast answer")
        add = self.check_additionals(got[0][0])
        url = "http://%s:%d/healthz" % (socket.inet_ntoa(add[K.T_A]), add[K.T_SRV][2])
        with urllib.request.urlopen(url, timeout=5) as r:
            h = json.loads(r.read())
        self.assertEqual((h["ok"], h["mdns"]["name"], h["mdns"]["address"]),
                         (True, self.name, LIVE))

    def test_c_goodbye_on_stop(self):
        s = mdns_socket(LIVE, 5353)
        try:
            self.srv.stop()
            got = collect(s, 3, lambda m: m["flags"] & 0x8000 and self.mine(m))
        finally:
            s.close()
        self.assertTrue(got, "no goodbye")
        self.assertEqual({r["ttl"] for r in got[0][0]["records"]}, {0})


# ---------------------------------------------------------------------------
# 11. optional: a real product file (KNXPROD_TEST_REAL_DATA)
# ---------------------------------------------------------------------------

@unittest.skipUnless(REAL and os.path.isfile(REAL), "set KNXPROD_TEST_REAL_DATA to a product file")
class TestRealData(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = _tmpdir()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        products = os.path.join(cls.tmp, "knxprod")
        os.makedirs(products)
        shutil.copy(REAL, os.path.join(products, os.path.basename(REAL)))
        t0 = time.monotonic()
        cls.srv = Server(products, os.path.join(cls.tmp, "index"))
        cls.addClassCleanup(cls.srv.stop)
        cls.report = {"initial_sync_seconds": round(time.monotonic() - t0, 1)}

    @classmethod
    def tearDownClass(cls):
        sys.stderr.write("\nREAL-DATA REPORT %s\n" % json.dumps(cls.report, indent=1))

    def timed(self, label, name, args):
        times, last = [], None
        for i in range(5):
            t0 = time.monotonic()
            last = self.srv.tool(name, args, rid=100 + i)
            times.append((time.monotonic() - t0) * 1000.0)
        self.report.setdefault("latency_ms", {})[label] = {
            "first": round(times[0], 1), "median_of_5": round(statistics.median(times), 1)}
        self.report.setdefault("body_bytes", {})[label] = last[2]
        self.assertLessEqual(last[2], K.RESPONSE_BUDGET)
        return last

    def test_a_indexed(self):
        h = self.srv.health()
        self.assertGreater(h["apps"], 0)
        self.assertEqual(h["file_errors"], 0)
        self.report["apps"] = h["apps"]

    def test_b_tools_list(self):
        times = []
        for i in range(5):
            t0 = time.monotonic()
            status, _, body = self.srv.mcp({"jsonrpc": "2.0", "id": i, "method": "tools/list"})
            times.append((time.monotonic() - t0) * 1000.0)
        tools = json.loads(body)["result"]["tools"]
        self.assertEqual(sum(1 for t in tools if t["name"].startswith("prod-")), 5)
        self.report.setdefault("latency_ms", {})["tools/list"] = {
            "median_of_5": round(statistics.median(times), 1)}
        self.report.setdefault("body_bytes", {})["tools/list"] = len(body)

    @unittest.skipUnless(REAL_APP, "set KNXPROD_TEST_APP_ID")
    def test_c_application_and_objects(self):
        res, o, _ = self.timed("prod-application", "prod-application", {"app_id": REAL_APP})
        self.assertFalse(res["isError"], o)
        self.report["products"] = [p["order_number"] for p in o["products"]]
        if REAL_OBJECTS:
            res, o, _ = self.timed("prod-comobjects", "prod-comobjects",
                                   {"app_id": REAL_APP, "numbers": REAL_OBJECTS})
            self.assertNotIn("missing_numbers", o)
            self.assertEqual(sorted(c["number"] for c in o["comobjects"]), sorted(REAL_OBJECTS))
            for c in o["comobjects"]:
                self.assertTrue(c["variants"])
                for v in c["variants"]:
                    self.assertRegex(v["flags"], r"^[C-][R-][W-][T-][U-][I-]$")
            self.report["comobjects"] = o["comobjects"]
        res, o, _ = self.timed("prod-parameters", "prod-parameters",
                               {"app_id": REAL_APP, "limit": 10})
        for p in o["parameters"]:
            if p.get("address") is not None:
                self.assertRegex(p["address_hex"], r"^0x[0-9A-F]{4,}$")
        self.report["parameter_example"] = o["parameters"][0] if o["parameters"] else None

    def test_d_every_result_within_budget(self):
        store = K.Store(self.srv.svc.data)
        worst = 0
        for app_id in sorted(store.index()["apps"]):
            rec = store.app(app_id)
            for name, key, extra, total in (
                    ("prod-comobjects", "comobjects", {}, len(rec["comobjects"])),
                    ("prod-parameters", "parameters", {"include_virtual": True},
                     len(rec["parameters"]))):
                off, seen = 0, 0
                while True:
                    r = K.handle_rpc({"jsonrpc": "2.0", "id": 123456, "method": "tools/call",
                                      "params": {"name": name, "arguments": dict(
                                          {"app_id": app_id, "offset": off, "limit": 200},
                                          **extra)}}, store)
                    body = len(K._dumps(r).encode())
                    worst = max(worst, body)
                    self.assertLessEqual(body, K.RESPONSE_BUDGET, (name, app_id, off))
                    self.assertFalse(r["result"]["isError"], (name, app_id, off))
                    o = json.loads(r["result"]["content"][0]["text"])
                    seen += len(o[key])
                    if o["next_offset"] is None:
                        break
                    off = o["next_offset"]
                self.assertEqual(seen, total, (name, app_id))
            r = K.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "prod-application",
                                         "arguments": {"app_id": app_id}}}, store)
            self.assertLessEqual(len(K._dumps(r).encode()), K.RESPONSE_BUDGET)
        self.report["largest_tools_call_body"] = worst


class TestRealModules(unittest.TestCase):
    """ETS module instances in a real product file (opt-in, see the docstring)."""

    @classmethod
    def setUpClass(cls):
        if not REAL_MODULES or not REAL_MODULE_APPS:
            raise unittest.SkipTest("set KNXPROD_TEST_MODULE_DATA and KNXPROD_TEST_MODULE_APPS")
        cls.tmp = _tmpdir()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.data = os.path.join(cls.tmp, "index")
        cls.summary = K.ingest_file(REAL_MODULES, cls.data)
        cls.store = K.Store(cls.data)
        cls.report = {}
        # the manufacturer's segment templates, straight from the file
        cls.templates = {}
        with K.SourceSet(REAL_MODULES, cls.tmp) as src:
            for m in src.members:
                if m.kind == "app":
                    with m.open() as f:
                        raws, _ = K.parse_application_xml(f)
                    for raw in raws:
                        rec_id = K.normalize_app_id(raw.full_id)
                        cls.templates[rec_id] = {k[len(raw.full_id) + 1:]: v
                                                 for k, v in raw.seg_data.items()}

    @classmethod
    def tearDownClass(cls):
        sys.stderr.write("\nREAL-MODULE REPORT %s\n" % json.dumps(cls.report, indent=1))

    def tool(self, name, args):
        r = K.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}}, self.store)
        self.assertLessEqual(len(K._dumps(r).encode()), K.RESPONSE_BUDGET)
        self.assertFalse(r["result"]["isError"], r)
        return json.loads(r["result"]["content"][0]["text"])

    def test_instances(self):
        for app in REAL_MODULE_APPS:
            rec = self.store.app(app)
            self.assertIsNotNone(rec, app)
            c = rec["counts"]
            # every module record placed: nothing skipped, nothing left unresolved
            self.assertGreater(c.get("module_instances", 0), 0, app)
            self.assertNotIn("module_unsupported", c, app)
            self.assertNotIn("module_records_skipped", c, app)
            rep = self.report.setdefault(app, {})
            rep.update({k: c.get(k) for k in ("parameters", "comobjects", "module_instances",
                                              "module_parameters", "module_comobjects",
                                              "texts_with_placeholders")})
            ids = [x["id"] for x in rec["parameters"] + rec["comobjects"]]
            self.assertEqual(len(ids), len(set(ids)), app)
            # per definition: an instance is the first one's layout, shifted by its
            # base arguments -- at most one distinct shift per numeric argument
            numeric = {m["id"]: [a for a, v in m["arguments"].items() if v != "text"]
                       for m in rec["modules"]}
            per_def = collections.defaultdict(dict)
            for p in rec["parameters"]:
                if p.get("module") and p.get("storage") == "memory":
                    iid = p["module"]["id"]
                    local = p["id"][len(iid) + 1:]
                    per_def[iid.split("_M-")[0]].setdefault(iid, {})[local] = p["offset"]
            for d, insts in per_def.items():
                layouts = list(insts.values())
                if len(layouts) < 2:
                    continue
                shifts = set()
                for lay in layouts[1:]:
                    common = set(layouts[0]) & set(lay)
                    self.assertTrue(common, (app, d))
                    diff = {lay[k] - layouts[0][k] for k in common}
                    self.assertLessEqual(len(diff), len(numeric[d]), (app, d, sorted(diff)[:5]))
                    self.assertTrue(any(diff), (app, d))         # not all at the same place
                    shifts.add(tuple(sorted(diff)))
                rep.setdefault("instance_shifts", {})[d] = sorted(shifts)[:4]
            labels = {p["module"]["label"] for p in rec["parameters"] if p.get("module")}
            self.assertTrue(all(lbl and "{{" not in lbl for lbl in labels), app)
            rep["labels"] = sorted(labels)[:6]

    def test_decode_an_instance_parameter(self):
        """The last instance's parameters (largest base) decoded through
        prod-decode: from the manufacturer's template where it holds the
        default (an independent check of the placement), otherwise from bytes
        built from the default."""
        for app in REAL_MODULE_APPS:
            rec = self.store.app(app)
            tpls = self.templates.get(app) or {}
            seg_ids = {s["id"]: s for s in rec["segments"]}
            order = rec["byte_order"]["used"]
            cands = []
            for p in reversed(rec["parameters"]):
                ty = rec["types"].get(p.get("type"), {})
                if (p.get("module") and p.get("storage") == "memory" and not p.get("union")
                        and ty.get("kind") in ("number", "enum") and p.get("size_bits")
                        and p["size_bits"] <= 32 and K._intval(p.get("default"))):
                    cands.append(p)
            self.assertTrue(cands, app)
            p, source = cands[0], "default"
            for q in cands:
                tpl = tpls.get(q["segment"])
                if tpl and K.extract_bits(tpl, q["offset"], q["bit_offset"], q["size_bits"],
                                          order) == int(q["default"]):
                    p, source = q, "template"
                    break
            nbytes = K._param_nbytes(p)
            want = int(p["default"]) & ((1 << p["size_bits"]) - 1)
            if source == "template":
                buf = tpls[p["segment"]][p["offset"]:p["offset"] + nbytes]
            else:
                shift = nbytes * 8 - p["bit_offset"] - p["size_bits"]
                buf = (want << shift).to_bytes(nbytes, "big")
            args = {"app_id": app, "data": buf.hex()}
            if seg_ids[p["segment"]].get("relative"):
                args.update(segment=p["segment"], address=p["offset"])
            else:
                args["address"] = p["address"]
            o = self.tool("prod-decode", args)
            got = {v["id"]: v for v in o["values"]}
            self.assertEqual(got[p["id"]]["value"], want, (app, p["id"]))
            self.assertEqual(got[p["id"]]["module"], p["module"]["label"])
            self.report.setdefault(app, {})["decoded"] = {
                "id": p["id"], "module": p["module"]["label"], "text": p["text"],
                "value": want, "bytes_from": source}
            self.report[app]["template_check_modules"] = rec["counts"].get(
                "template_check_modules")

    def test_tools_within_budget(self):
        for app in REAL_MODULE_APPS:
            rec = self.store.app(app)
            label = next(p["module"]["label"] for p in rec["parameters"] if p.get("module"))
            for name, args in (("prod-parameters", {"module": label}),
                               ("prod-comobjects", {"module": label}),
                               ("prod-application", {})):
                o = self.tool(name, dict(args, app_id=app))
                if name != "prod-application":
                    self.assertGreater(o["total"], 0, (app, name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
