from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class OPMLEntry:
    title: str | None
    feed_url: str
    html_url: str | None
    category: str | None


def parse_opml(file_path: Path) -> list[OPMLEntry]:
    """Walk an OPML file and return all feed entries.

    Handles:
    - Flat <body>/<outline xmlUrl=...> layout (Apple Podcasts export)
    - Nested <outline text="Category"><outline xmlUrl=...></outline></outline>
      layout (Overcast, Pocket Casts, AntennaPod). The category text becomes
      the OPMLEntry.category for each child entry.
    - Skips <outline> nodes without an xmlUrl (they're folders or comments)
    """
    tree = ET.parse(file_path)
    root = tree.getroot()

    body = root.find("body")
    if body is None:
        return []

    entries: list[OPMLEntry] = []

    def _entry_from(outline: ET.Element, category: str | None) -> OPMLEntry | None:
        feed_url = outline.get("xmlUrl")
        if not feed_url:
            return None
        title = outline.get("text") or outline.get("title") or feed_url
        return OPMLEntry(
            title=title,
            feed_url=feed_url,
            html_url=outline.get("htmlUrl"),
            category=category,
        )

    for child in body:
        if child.tag != "outline":
            continue
        # If this outline has an xmlUrl it's a direct feed entry (flat layout).
        if child.get("xmlUrl"):
            e = _entry_from(child, None)
            if e:
                entries.append(e)
        else:
            # It's a category folder; its text attribute names the category.
            cat_name = child.get("text") or child.get("title")
            for grandchild in child:
                if grandchild.tag != "outline":
                    continue
                e = _entry_from(grandchild, cat_name)
                if e:
                    entries.append(e)

    return entries
