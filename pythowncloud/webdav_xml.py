"""
WebDAV XML response builders for PROPFIND requests.
Uses stdlib xml.etree.ElementTree (no external dependencies).
"""

import mimetypes
from datetime import datetime
from typing import Any
from xml.etree import ElementTree as ET


# RFC 1123 date formatting for WebDAV
def _rfc1123(dt: datetime) -> str:
    """Format datetime in RFC 1123 format (e.g., 'Sat, 01 Mar 2025 12:00:00 GMT')."""
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _mime_type(filename: str) -> str:
    """Guess MIME type for a filename."""
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def build_propfind_response(
    items: list[dict[str, Any]],
    base_href: str,
    include_self: bool = True,
    self_item: dict[str, Any] | None = None,
) -> str:
    """
    Build a WebDAV PROPFIND 207 Multi-Status response.

    Args:
        items: List of file/directory rows from db.list_directory() or db.get_file_row().
               Each row has: path, filename, size, is_dir, modified_at, checksum.
        base_href: The full `/dav/...` URL of the directory being listed.
        include_self: If True, prepend a <response> for the directory itself.
        self_item: Row for the directory itself (required if include_self=True).

    Returns:
        UTF-8 XML string ready for HTTP response body.
    """
    # Register the DAV namespace
    DAV_NS = "DAV:"
    ET.register_namespace("D", DAV_NS)

    root = ET.Element(f"{{{DAV_NS}}}multistatus")
    root.set("xmlns:D", DAV_NS)

    # Add the directory itself if requested
    if include_self and self_item:
        response = _build_response_element(self_item, base_href, DAV_NS)
        root.append(response)

    # Add each child item
    for item in items:
        # Construct href for this item
        item_href = base_href
        if not item_href.endswith("/"):
            item_href += "/"
        item_href += item["filename"]
        if item["is_dir"]:
            item_href += "/"

        response = _build_response_element(item, item_href, DAV_NS)
        root.append(response)

    # Return as UTF-8 XML string
    xml_str = ET.tostring(root, encoding="utf-8", method="xml")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_str.decode("utf-8")}'


def _build_response_element(
    item: dict[str, Any], href: str, dav_ns: str
) -> ET.Element:
    """
    Build a single <D:response> element for one file/directory.

    Args:
        item: Row with keys: path, filename, size, is_dir, modified_at, checksum.
        href: Full URL path for this item (with trailing / for directories).
        dav_ns: The DAV namespace URI.

    Returns:
        An ET.Element ready to append to the multistatus root.
    """
    response = ET.Element(f"{{{dav_ns}}}response")

    # <D:href>
    href_elem = ET.SubElement(response, f"{{{dav_ns}}}href")
    href_elem.text = href

    # <D:propstat>
    propstat = ET.SubElement(response, f"{{{dav_ns}}}propstat")

    # <D:prop> — the actual properties
    prop = ET.SubElement(propstat, f"{{{dav_ns}}}prop")

    # displayname (filename)
    displayname = ET.SubElement(prop, f"{{{dav_ns}}}displayname")
    displayname.text = item["filename"]

    # resourcetype (collection for dirs, empty for files)
    resourcetype = ET.SubElement(prop, f"{{{dav_ns}}}resourcetype")
    if item["is_dir"]:
        ET.SubElement(resourcetype, f"{{{dav_ns}}}collection")

    # getcontentlength (size, files only)
    if not item["is_dir"]:
        getcontentlength = ET.SubElement(prop, f"{{{dav_ns}}}getcontentlength")
        getcontentlength.text = str(item["size"])

        # getcontenttype (mime type, files only)
        getcontenttype = ET.SubElement(prop, f"{{{dav_ns}}}getcontenttype")
        getcontenttype.text = _mime_type(item["filename"])

    # getlastmodified (RFC 1123 format)
    modified_str = item["modified_at"]
    if isinstance(modified_str, str):
        # Parse ISO format datetime string
        modified_dt = datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
    else:
        modified_dt = modified_str

    getlastmodified = ET.SubElement(prop, f"{{{dav_ns}}}getlastmodified")
    getlastmodified.text = _rfc1123(modified_dt)

    # getetag (short checksum hash for files, path-based for dirs)
    getetag = ET.SubElement(prop, f"{{{dav_ns}}}getetag")
    if item["is_dir"]:
        getetag.text = f'"dir:{item["path"]}"'
    else:
        # Use first 16 chars of checksum for brevity
        checksum = item.get("checksum", "")
        if checksum:
            getetag.text = f'"sha256:{checksum[:16]}"'
        else:
            getetag.text = f'"file:{item["path"]}"'

    # <D:status>
    status = ET.SubElement(propstat, f"{{{dav_ns}}}status")
    status.text = "HTTP/1.1 200 OK"

    return response
