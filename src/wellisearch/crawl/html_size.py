"""Bound and capture serialized HTML atomically inside the browser process."""

CAPTURE_BOUNDED_HTML_JS = """
(limit) => {
  const voidTags = new Set([
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr"
  ]);
  const htmlNamespace = "http://www.w3.org/1999/xhtml";
  const shownNodes = NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT |
    NodeFilter.SHOW_COMMENT | NodeFilter.SHOW_DOCUMENT_TYPE;
  const walkers = [document.createTreeWalker(document, shownNodes)];
  let bytes = 0;
  const addString = (value, escapeText = false, escapeAttribute = false) => {
    for (let i = 0; i < value.length; i++) {
      const code = value.charCodeAt(i);
      if (escapeText && code === 38) {
        bytes += 5;
      } else if (escapeText && code === 60) {
        bytes += 4;
      } else if (escapeText && code === 62) {
        bytes += 4;
      } else if (escapeText && code === 160) {
        bytes += 6;
      } else if (escapeAttribute && code === 34) {
        bytes += 6;
      } else if (code < 0x80) {
        bytes += code === 0 ? 3 : 1;
      } else if (code < 0x800) {
        bytes += 2;
      } else if (
        code >= 0xD800 && code <= 0xDBFF &&
        i + 1 < value.length &&
        value.charCodeAt(i + 1) >= 0xDC00 && value.charCodeAt(i + 1) <= 0xDFFF
      ) {
        bytes += 4;
        i++;
      } else {
        bytes += 3;
      }
      if (bytes > limit) return true;
    }
    return false;
  };
  while (walkers.length) {
    const walker = walkers[walkers.length - 1];
    const node = walker.nextNode();
    if (!node) {
      walkers.pop();
      continue;
    }
    if (node.nodeType === Node.ELEMENT_NODE) {
      const name = node.nodeName;
      bytes += 2;
      if (addString(name)) return null;
      for (const attribute of node.attributes) {
        bytes += 4;
        if (addString(attribute.name) || addString(attribute.value, true, true)) {
          return null;
        }
      }
      const isVoid = node.namespaceURI === htmlNamespace && voidTags.has(node.localName);
      if (!isVoid) {
        bytes += 3;
        if (addString(name)) return null;
      }
      if (node.localName === "template" && node.content) {
        walkers.push(document.createTreeWalker(node.content, shownNodes));
      }
    } else if (node.nodeType === Node.TEXT_NODE) {
      const parent = node.parentElement;
      const raw = parent && parent.namespaceURI === htmlNamespace &&
        (parent.localName === "script" || parent.localName === "style");
      if (addString(node.nodeValue ?? "", !raw)) return null;
    } else if (node.nodeType === Node.COMMENT_NODE) {
      bytes += 7;
      if (addString(node.nodeValue ?? "")) return null;
    } else if (node.nodeType === Node.DOCUMENT_TYPE_NODE) {
      bytes += 10;
      if (addString(node.name)) return null;
      if (node.publicId) {
        bytes += 9;
        if (addString(node.publicId)) return null;
        if (node.systemId) {
          bytes += 3;
          if (addString(node.systemId)) return null;
          bytes += 1;
        } else {
          bytes += 1;
        }
      } else if (node.systemId) {
        bytes += 9;
        if (addString(node.systemId)) return null;
        bytes += 1;
      }
      bytes += 1;
    }
    if (bytes > limit) return null;
  }
  const root = document.documentElement;
  const doctype = document.doctype;
  let prefix = "";
  if (doctype) {
    prefix = `<!DOCTYPE ${doctype.name}`;
    if (doctype.publicId) {
      prefix += ` PUBLIC "${doctype.publicId}"`;
      if (doctype.systemId) prefix += ` "${doctype.systemId}"`;
    } else if (doctype.systemId) {
      prefix += ` SYSTEM "${doctype.systemId}"`;
    }
    prefix += ">";
  }
  const html = prefix + (root?.outerHTML ?? "");
  bytes = 0;
  if (addString(html)) return null;
  return html;
}
"""
