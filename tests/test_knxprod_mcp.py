#!/usr/bin/env python3
"""Tests for knxprod_mcp.py.  Standard library only:

    python3 tests/test_knxprod_mcp.py            (or: python3 -m unittest discover -s tests)

The fixture under tests/fixture/ is synthetic (fictitious manufacturer M-FFF0)
and written for these tests.  HTTP tests listen on 127.0.0.1 with a free port.

Optional test against a real product file you own (never committed):

    KNXPROD_TEST_REAL_DATA=/path/to/file.knxprod python3 tests/test_knxprod_mcp.py
      KNXPROD_TEST_APP_ID=<10 hex digits>     an ApplicationId inside that file
      KNXPROD_TEST_OBJECTS=0,2,16             com-object numbers it must return
"""

import base64
import contextlib
import http.client
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import knxprod_mcp as K  # noqa: E402

FIXTURE = os.path.join(HERE, "fixture")
APP_XML = os.path.join(FIXTURE, "M-FFF0", "M-FFF0_A-1234-56-ABCD.xml")
APP2_XML = os.path.join(FIXTURE, "M-FFF0", "M-FFF0_A-00AB-01-1111-OFFF1.xml")
REAL = os.environ.get("KNXPROD_TEST_REAL_DATA")
REAL_APP = os.environ.get("KNXPROD_TEST_APP_ID")
REAL_OBJECTS = [int(x) for x in (os.environ.get("KNXPROD_TEST_OBJECTS") or "").split(",")
                if x.strip()]
APP1 = "FFF0123456"
APP2 = "FFF000AB01"
ENV_KEYS = ("KNXPROD_DIR", "KNXPROD_DATA", "KNXPROD_BIND", "KNXPROD_PORT",
            "KNXPROD_ALLOWED_HOSTS", "KNXPROD_UPLOAD_ORIGINS", "KNXPROD_SCAN_INTERVAL",
            "KNXPROD_ALLOW_ANY_BIND")


def _tmpdir():
    return tempfile.mkdtemp(prefix="knxprod-mcp-test-")


def _read(path):
    with open(path, "rb") as f:
        return f.read()


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
             "app2": ("M-FFF0/M-FFF0_A-00AB-01-1111-OFFF1.xml", app2)}
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
    """The real service in a thread on 127.0.0.1 and a free port."""

    def __init__(self, products, data, **cfg):
        self.lines = []
        c = {"products": products, "data": data, "bind": "127.0.0.1", "port": 0,
             "scan_interval": 0}
        c.update(cfg)
        self.svc = K.Service(c, log=self.lines.append)
        self.svc.initial_sync()
        self.svc.activate()
        self.svc.start_scanner()
        self.port = self.svc.port
        self.thread = threading.Thread(target=self.svc.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.svc.httpd.shutdown()
        self.svc.close()
        self.thread.join(5)

    def request(self, method, path, body=b"", headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host if host is not None else "127.0.0.1:%d" % self.port)
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
            for phrase in ("read-only", "private network", "SLOTS", "size_bits", "licensed"):
                self.assertIn(phrase, res["instructions"])

    def test_notifications_and_unknown(self):
        self.assertIsNone(K.handle_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"},
                                       self.store))
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertEqual(self.rpc("resources/list")["error"]["code"], -32601)
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
        self.assertEqual(o["total"], 2)
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
            with contextlib.redirect_stderr(open(os.devnull, "w")):
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
        generated = json.load(open(os.path.join(self.data, "index.json")))["generated"]
        s = self.sync()
        self.assertEqual((s["added"], s["changed"], s["unchanged"]), ([], [], 1))
        t = time.time() - 30
        os.utime(os.path.join(self.products, "a.knxprod"), (t, t))       # touched, same bytes
        s = self.sync()
        self.assertEqual((s["changed"], s["unchanged"]), ([], 1))
        self.assertEqual(json.load(open(os.path.join(self.data, "index.json")))["generated"],
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
        name = lambda: json.load(open(os.path.join(self.data, "apps", APP2 + ".json")))["name"]
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
# 9. optional: a real product file (KNXPROD_TEST_REAL_DATA)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
