from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch, Mock
import xml.etree.ElementTree as ET

from newsrss.generate import (load_config, render_feed, deduplicate, extract_image,
    generate, enrich, MEDIA, parse_news, resolve_article)
from newsrss.network import safe_url, resolve_public, Client

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
ITEM = dict(id="stable", title="多摩 & ニュース", source="地域新聞", url="https://example.com/a",
    google_url="https://news.google.com/rss/articles/test", published=NOW.isoformat(),
    description='<script>alert(1)</script> & 本文', image="https://example.com/image.jpg?a=1&b=2")
RSS = b'''<rss version="2.0"><channel><title>test</title><link>https://example.com/</link><description>test</description><item><title>News</title><link>https://example.com/a</link><pubDate>Tue, 22 Sep 2026 00:00:00 GMT</pubDate><source>Paper</source><description>Summary</description></item></channel></rss>'''


class Tests(unittest.TestCase):
    def test_config(self):
        self.assertEqual(len(load_config("locations.yml")), 3)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yml"
            for data in ['locations: []', 'locations: [{name: X, slug: ../bad, query: X}]',
                         'locations: [{name: X, slug: x, query: X}, {name: Y, slug: x, query: Y}]']:
                p.write_text(data)
                with self.assertRaises(ValueError): load_config(p)

    def test_xml(self):
        loc = load_config("locations.yml")[2]
        root = ET.fromstring(render_feed(loc, [ITEM], "https://user.github.io/repo"))
        self.assertEqual(root.findtext("channel/item/title"), ITEM["title"])
        self.assertEqual(root.find("channel/item/" + "{" + MEDIA + "}thumbnail").get("url"), ITEM["image"])
        self.assertIn("&lt;script&gt;", root.findtext("channel/item/description"))
        self.assertEqual(root.find("channel/item/guid").get("isPermaLink"), "false")

    def test_dedupe(self):
        duplicate = dict(ITEM, url=ITEM["url"] + "?utm_source=x")
        other = dict(ITEM, source="別の新聞", url="https://other.example/b", google_url="https://news.google.com/other")
        title_duplicate = dict(ITEM, url="https://example.com/new", google_url="https://news.google.com/new")
        self.assertEqual(len(deduplicate([ITEM, duplicate, other, title_duplicate])), 2)

    def test_image(self):
        self.assertEqual(extract_image('<meta name="twitter:image" content="/tw.jpg"><meta property="og:image" content="/og.jpg">', 'https://example.com/a'), 'https://example.com/og.jpg')
        self.assertEqual(extract_image('<meta property="og:image" content="http://127.0.0.1/x"><meta name="twitter:image" content="//example.com/tw.jpg">', 'https://example.com'), 'https://example.com/tw.jpg')
        self.assertIsNone(extract_image('<html>none</html>', 'https://example.com'))

    def test_safety(self):
        for url in ['file:///x', 'ftp://example.com', 'http://localhost', 'http://127.0.0.1', 'http://10.0.0.1',
                    'http://169.254.169.254/', 'http://[::1]/', 'http://[::ffff:127.0.0.1]',
                    'http://224.0.0.1', 'https://user:pass@example.com', 'https://example.com:9000', 'http://x.local/']:
            with self.subTest(url=url), self.assertRaises(ValueError): safe_url(url)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.1', 80))]):
            with self.assertRaises(ValueError): resolve_public('https://example.com')

    def test_redirect_and_pinning(self):
        response = Mock(status=302, headers={'Location': 'http://127.0.0.1/secret'})
        pool = Mock()
        pool.request.return_value = response
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]), patch('urllib3.HTTPSConnectionPool', return_value=pool) as factory:
            with self.assertRaises(ValueError): Client(delay=0).fetch('https://example.com/a')
            self.assertEqual(factory.call_args.args[0], '93.184.216.34')
            self.assertEqual(factory.call_args.kwargs['server_hostname'], 'example.com')
            self.assertEqual(pool.request.call_count, 1)

    def test_cache_failures(self):
        client = Mock()
        client.fetch.side_effect = TimeoutError('timeout')
        cache = {}
        with patch('newsrss.generate.resolve_public', return_value=['93.184.216.34']):
            for days in [0, 0, 1, 3, 6, 10]:
                item = dict(ITEM, google_url='https://example.com/a', image=None)
                enrich(item, cache, client, NOW + timedelta(days=days))
                self.assertEqual(item['url'], ITEM['url'])
        self.assertEqual(client.fetch.call_count, 3)
        self.assertEqual(cache['https://example.com/a']['attempts'], 3)

    def test_generation_repeat_and_retention(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'locations.yml').write_bytes(Path('locations.yml').read_bytes())
            client = Mock()
            client.fetch.return_value = ('https://news.google.com/rss', RSS)
            generate(root, 'https://user.github.io/repo', 0, client, NOW)
            before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            generate(root, 'https://user.github.io/repo', 0, client, NOW + timedelta(hours=1))
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            self.assertEqual(len(list((root / 'feeds').glob('*.xml'))), 3)
            client.fetch.side_effect = TimeoutError()
            with self.assertRaises(RuntimeError): generate(root, '', 0, client, NOW + timedelta(days=31))
            self.assertEqual(json.loads((root / 'data/tama.json').read_text()), [])

    def test_google_decoder(self):
        client = Mock()
        client.fetch.side_effect = [('https://news.google.com/rss/articles/test', b'<div data-n-a-sg="sig" data-n-a-ts="123"></div>'),
            ('https://news.google.com/rpc', json.dumps([["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://example.com/article"])]]).encode())]
        self.assertEqual(resolve_article(client, ITEM['google_url']), 'https://example.com/article')

    def test_success_cache_not_refetched(self):
        client = Mock()
        cache = {ITEM['google_url']: {'image': ITEM['image'], 'url': ITEM['url'], 'attempts': 1}}
        item = dict(ITEM, image=None)
        enrich(item, cache, client, NOW + timedelta(days=20))
        client.fetch.assert_not_called()
        self.assertEqual(item['image'], ITEM['image'])

    def test_guid_survives_changed_source_link(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'locations.yml').write_bytes(Path('locations.yml').read_bytes())
            client = Mock()
            client.fetch.return_value = ('https://news.google.com/rss', RSS)
            generate(root, '', 0, client, NOW)
            first = json.loads((root / 'data/tama.json').read_text(encoding='utf-8'))[0]
            client.fetch.return_value = ('https://news.google.com/rss', RSS.replace(b'https://example.com/a', b'https://example.com/new'))
            generate(root, '', 0, client, NOW)
            second = json.loads((root / 'data/tama.json').read_text(encoding='utf-8'))
            self.assertEqual(len(second), 1)
            self.assertEqual(first['id'], second[0]['id'])

    def test_http_response_limit(self):
        response = Mock(status=200, headers={})
        response.stream.return_value = iter([b'x' * 12])
        pool = Mock()
        pool.request.return_value = response
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]), patch('urllib3.HTTPSConnectionPool', return_value=pool):
            with self.assertRaises(ValueError): Client(delay=0).fetch('https://example.com', limit=10)
            response.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
