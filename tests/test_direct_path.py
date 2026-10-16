"""The 'direct' path: how viewers reach a share without video crossing Cloudflare —
URL selection (own domain > quick tunnel > LAN), the per-share access key, and UPnP
forwarding of the WebRTC UDP port, tested against a fake router that behaves like the
TP-Link Deco (secure mode: silently drops mappings for other hosts).

    python3 -m unittest discover -s tests -v
"""
import os
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from test_daemon import d

WAN = "urn:schemas-upnp-org:service:WANIPConnection:1"


class Urls(unittest.TestCase):
    def setUp(self):
        d.state["key"] = "abc123"

    def test_priority_own_domain_then_tunnel_then_lan(self):
        with mock.patch.dict(os.environ, {"DS_PUBLIC_URL": "https://share.example.com"}):
            w, ws, via = d.share_urls("192.168.1.5", "https://x-y.trycloudflare.com")
        self.assertEqual((w, ws, via), ("https://share.example.com/screen/whep?k=abc123",
                                        "wss://share.example.com/screen/ws?k=abc123", "public"))
        with mock.patch.object(d, "public_url", return_value=None):
            w, ws, via = d.share_urls("192.168.1.5", "https://x-y.trycloudflare.com")
            self.assertEqual(via, "tunnel")
            self.assertTrue(ws.startswith("wss://x-y.trycloudflare.com/screen/ws?k="))
            w, ws, via = d.share_urls("192.168.1.5", None)
            self.assertEqual(w, "http://192.168.1.5:48890/screen/whep?k=abc123")
            self.assertEqual(via, "lan")

    def test_public_url_must_be_plain_https_origin(self):
        for bad in ("http://share.example.com", "https://x.com/path", "javascript:1", "https://"):
            with mock.patch.dict(os.environ, {"DS_PUBLIC_URL": bad}):
                self.assertIsNone(d.public_url(), bad)
        with mock.patch.dict(os.environ, {"DS_PUBLIC_URL": "https://share.example.com:8443/"}):
            self.assertEqual(d.public_url(), "https://share.example.com:8443")

    def test_key(self):
        self.assertTrue(d.key_ok("/screen/ws?k=abc123"))
        self.assertFalse(d.key_ok("/screen/ws?k=abc12"))
        self.assertFalse(d.key_ok("/screen/ws"))
        d.state["key"] = None                     # no share running -> nothing is valid
        self.assertFalse(d.key_ok("/screen/ws?k="))

    def test_signaled_whep_url_passes_the_viewer_proxy_check(self):
        with mock.patch.object(d, "public_url", return_value=None):
            self.assertTrue(d.whep_target_ok(d.lan_whep("192.168.1.50")))
            for tun in (None, "https://x-y.trycloudflare.com"):
                self.assertTrue(d.whep_target_ok(d.share_urls("10.0.0.2", tun)[0]))


class FakeIGD:
    """Minimal UPnP Internet Gateway: description XML + WANIPConnection SOAP."""

    def __init__(self, ext_ip="76.1.2.3", secure_mode_target=None, permanent_only=False):
        self.ext_ip, self.permanent_only = ext_ip, permanent_only
        self.secure_mode_target = secure_mode_target   # if set, mappings "stick" to this host
        self.table, self.calls = {}, []
        igd = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0">'
                        '<device><deviceList><device><serviceList><service>'
                        '<serviceType>%s</serviceType><controlURL>/ctl</controlURL>'
                        '</service></serviceList></device></deviceList></device></root>' % WAN)
                self._reply(200, body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
                action = re.search(r"#(\w+)", self.headers["SOAPAction"]).group(1)
                args = dict(re.findall(r"<(New\w+)>([^<]*)</New\w+>", raw))
                igd.calls.append((action, args))
                if action == "GetExternalIPAddress":
                    return self._ok(action, NewExternalIPAddress=igd.ext_ip)
                if action == "AddPortMapping":
                    if igd.permanent_only and args["NewLeaseDuration"] != "0":
                        return self._fault("725")
                    key = (args["NewExternalPort"], args["NewProtocol"])
                    igd.table[key] = igd.secure_mode_target or args["NewInternalClient"]
                    return self._ok(action)
                if action == "GetSpecificPortMappingEntry":
                    who = igd.table.get((args["NewExternalPort"], args["NewProtocol"]))
                    return self._ok(action, NewInternalClient=who) if who else self._fault("714")
                if action == "DeletePortMapping":
                    igd.table.pop((args["NewExternalPort"], args["NewProtocol"]), None)
                    return self._ok(action)
                self._fault("401")

            def _ok(self, action, **out):
                inner = "".join("<%s>%s</%s>" % (k, v, k) for k, v in out.items())
                self._reply(200, '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                            '<s:Body><u:%sResponse xmlns:u="%s">%s</u:%sResponse></s:Body>'
                            '</s:Envelope>' % (action, WAN, inner, action))

            def _fault(self, code):
                self._reply(500, '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                            '<s:Body><s:Fault><detail><UPnPError><errorCode>%s</errorCode>'
                            '</UPnPError></detail></s:Fault></s:Body></s:Envelope>' % code)

            def _reply(self, code, body):
                b = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/desc.xml" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class UPnP(unittest.TestCase):
    ME = "192.168.1.50"

    def run_map(self, igd):
        with mock.patch.object(d, "_upnp_discover", return_value=igd.url), \
             mock.patch.object(d, "lan_ip", return_value=self.ME):
            return d.upnp_map()

    def test_maps_our_own_udp_port_and_releases_it(self):
        igd = FakeIGD()
        try:
            st = self.run_map(igd)
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["ext_ip"], "76.1.2.3")
            self.assertEqual(igd.table, {(str(d.WEBRTC_UDP), "UDP"): self.ME})
            add = [a for act, a in igd.calls if act == "AddPortMapping"][0]
            self.assertEqual(add["NewInternalPort"], str(d.WEBRTC_UDP))   # ICE candidate port == external
            self.assertEqual(add["NewLeaseDuration"], str(d.UPNP_LEASE))
            d.upnp_release()
            self.assertEqual(igd.table, {})
        finally:
            igd.close()
            d.state.pop("upnp_ctl", None)

    def test_secure_mode_silent_drop_is_detected(self):
        igd = FakeIGD(secure_mode_target="192.168.1.99")
        try:
            st = self.run_map(igd)
        finally:
            igd.close()
        self.assertFalse(st["ok"])
        self.assertIn("192.168.1.99", st["why"])

    def test_permanent_only_router_gets_lease_zero(self):
        igd = FakeIGD(permanent_only=True)
        try:
            st = self.run_map(igd)
        finally:
            igd.close()
            d.state.pop("upnp_ctl", None)
        self.assertTrue(st["ok"], st)
        leases = [a["NewLeaseDuration"] for act, a in igd.calls if act == "AddPortMapping"]
        self.assertEqual(leases, [str(d.UPNP_LEASE), "0"])

    def test_double_nat_is_reported_not_mapped(self):
        igd = FakeIGD(ext_ip="100.72.4.9")          # carrier-grade NAT range
        try:
            st = self.run_map(igd)
        finally:
            igd.close()
        self.assertFalse(st["ok"])
        self.assertIn("CGNAT", st["why"])
        self.assertEqual(igd.table, {})

    def test_no_router(self):
        with mock.patch.object(d, "_upnp_discover", return_value=None):
            st = d.upnp_map()
        self.assertFalse(st["ok"])


if __name__ == "__main__":
    unittest.main()
