from pathlib import Path
import socket
import unittest
from unittest.mock import Mock, patch

from zanbot.tools import ToolError, check_url, default_tools, html_to_text


def resolves_to(*ips):
    return lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]


PUBLIC = resolves_to("93.184.216.34")


def page(body, content_type="text/html; charset=utf-8", status=200, location=None):
    data = body.encode("utf-8") if isinstance(body, str) else body
    response = Mock(status_code=status, is_redirect=location is not None,
                    headers={"content-type": content_type, **({"location": location} if location else {})},
                    encoding="utf-8" if "charset=utf-8" in content_type else None)
    response.iter_content = Mock(return_value=iter([data]))
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


def fetch(url="https://example.com/"):
    return default_tools(Path(".")).execute({"type": "tool_use", "id": "t", "name": "web_fetch",
                                             "input": {"url": url}})


class CheckUrlTests(unittest.TestCase):
    def test_refuses_other_schemes(self):
        for url in ("file:///C:/Windows/win.ini", "ftp://example.com/", "javascript:alert(1)", "https://"):
            with self.subTest(url=url), self.assertRaisesRegex(ToolError, "Only http"):
                check_url(url)

    def test_refuses_local_and_private_addresses(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "100.64.0.1",
                   "0.0.0.0", "::1", "fe80::1", "::ffff:127.0.0.1", "fd00::1"):
            with self.subTest(ip=ip), patch("socket.getaddrinfo", resolves_to(ip)):
                with self.assertRaisesRegex(ToolError, "local or private"):
                    check_url("http://looks-public.example/")

    def test_one_private_address_among_several_is_enough_to_refuse(self):
        with patch("socket.getaddrinfo", resolves_to("93.184.216.34", "127.0.0.1")):
            with self.assertRaises(ToolError):
                check_url("https://example.com/")

    def test_public_address_is_fine(self):
        with patch("socket.getaddrinfo", PUBLIC):
            check_url("https://example.com/path?q=1")

    def test_unknown_host(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror()):
            with self.assertRaisesRegex(ToolError, "Cannot find the website"):
                check_url("https://no-such-host.invalid/")


class HtmlTests(unittest.TestCase):
    def test_title_text_and_no_scripts(self):
        title, text = html_to_text(
            "<html><head><title> 天气\n预报 </title><style>p{color:red}</style></head>"
            "<body><script>alert('x')</script><h1>北京</h1><p>晴，  25&deg;C</p>"
            "<ul><li>周一</li><li>周二</li></ul><svg><text>icon</text></svg></body></html>")
        self.assertEqual(title, "天气 预报")
        self.assertEqual(text, "北京\n\n晴， 25°C\n\n- 周一\n\n- 周二")


@patch("socket.getaddrinfo", PUBLIC)
@patch("zanbot.tools.requests.get")
class WebFetchTests(unittest.TestCase):
    def test_html_page(self, get):
        get.return_value = page("<title>Hi</title><p>Hello</p>")
        self.assertEqual(fetch(), ("URL: https://example.com/\nTitle: Hi\n\nHello", False))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_plain_text_and_json(self, get):
        for kind in ("text/plain", "application/json"):
            with self.subTest(kind=kind):
                get.return_value = page('{"a": "<b>"}', kind)
                self.assertEqual(fetch()[0], 'URL: https://example.com/\n\n{"a": "<b>"}')

    def test_charset_from_meta_tag(self, get):
        get.return_value = page('<meta charset="gbk"><p>你好</p>'.encode("gbk"), "text/html")
        self.assertIn("你好", fetch()[0])

    def test_follows_redirects_but_checks_each_hop(self, get):
        get.side_effect = [page("", location="/new"), page("<p>moved</p>")]
        self.assertEqual(fetch()[0], "URL: https://example.com/new\n\nmoved")
        get.side_effect = [page("", location="http://internal.example/")]
        with patch("socket.getaddrinfo", side_effect=[PUBLIC("example.com", 443)[:1], resolves_to("10.0.0.1")("", 80)]):
            output, is_error = fetch()
        self.assertTrue(is_error)
        self.assertIn("local or private", output)

    def test_too_many_redirects(self, get):
        get.side_effect = lambda *a, **k: page("", location="/again")
        output, is_error = fetch()
        self.assertTrue(is_error)
        self.assertIn("Too many redirects", output)

    def test_errors_and_unreadable_content(self, get):
        for response, message in ((page("nope", status=404), "HTTP 404"),
                                  (page(b"\x89PNG", "image/png"), "Cannot read image/png")):
            with self.subTest(message=message):
                get.return_value = response
                output, is_error = fetch()
                self.assertTrue(is_error)
                self.assertIn(message, output)

    def test_network_failure(self, get):
        import requests

        get.side_effect = requests.ConnectionError()
        output, is_error = fetch()
        self.assertTrue(is_error)
        self.assertIn("Could not fetch", output)

    def test_needs_permission_but_allows_always(self, get):
        registry = default_tools(Path("."))
        self.assertTrue(registry.needs_permission("web_fetch"))
        self.assertTrue(registry.allows_always("web_fetch"))


if __name__ == "__main__":
    unittest.main()
