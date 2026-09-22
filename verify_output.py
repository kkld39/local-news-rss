"""Offline sanity check for generated production files."""
from pathlib import Path
import xml.etree.ElementTree as ET
import feedparser
from newsrss.generate import load_config, MEDIA


def main():
    for loc in load_config('locations.yml'):
        path = Path('feeds') / (loc['slug'] + '.xml')
        tree = ET.fromstring(path.read_bytes())
        feed = feedparser.parse(path.read_bytes())
        assert not feed.bozo, feed.get('bozo_exception')
        items = tree.findall('channel/item')
        assert items, f'{path}: empty'
        assert len({i.findtext('guid') for i in items}) == len(items)
        images = sum(i.find(f'{{{MEDIA}}}thumbnail') is not None for i in items)
        print(f'{path}: valid XML, {len(items)} articles, {images} thumbnails')
    assert Path('index.html').exists()
    print('index.html: exists')


if __name__ == '__main__':
    main()
