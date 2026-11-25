#!/usr/bin/env python3
"""
Add supplier fields to every symbol in KiCad symbol libraries (.kicad_sym).

Fields added to each top-level symbol (always, even if the search finds nothing):
    "Digikey Part Number", "Digikey Link", "LCSC Part Number", "LCSC Link"

Search:
  * The part number is taken from the first non-empty property of:
      Manufacturer_Part_Number, MPN, Manufacturer Part Number,
      MANUFACTURER_PART_NUMBER, MFR_PN, Value  (falls back to the symbol name)
  * LCSC   - searched through the public JLCPCB parts search (same C-numbers
             as LCSC). No key needed.
  * DigiKey - searched through the official DigiKey Product Information API v4.
             Needs a (free) app from https://developer.digikey.com with the
             "Product Information V4" API. Set these environment variables:
                 DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET
             Optional: DIGIKEY_LOCALE_SITE (default US), DIGIKEY_CURRENCY (USD)
             Without them, the DigiKey fields are still added, left empty.
  * Only an exact part-number match is accepted (case/whitespace-insensitive).
    If the symbol has a manufacturer field, a matching brand is preferred,
    then the listing with the most stock.

Existing values are never overwritten unless --force is given. Empty existing
fields are filled in when a match is found.

Run with KiCad's bundled Python if you don't have Python installed, e.g.:
    & "C:\\Program Files\\KiCad\\9.0\\bin\\python.exe" scripts\\add_supplier_fields.py
"""

import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FIELDS = ["Digikey Part Number", "Digikey Link", "LCSC Part Number", "LCSC Link"]
VENDOR_FIELDS = {
    "digikey": ("Digikey Part Number", "Digikey Link"),
    "lcsc": ("LCSC Part Number", "LCSC Link"),
}
MPN_KEYS = ["Manufacturer_Part_Number", "MPN", "Manufacturer Part Number",
            "MANUFACTURER_PART_NUMBER", "MFR_PN", "Value"]
MFR_KEYS = ["Manufacturer_Name", "Manufacturer", "MANUFACTURER", "MFR", "Mfr"]
BAD_VALUES = {"", "N/A", "NA", "~", "-"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")


# --------------------------------------------------------------------------
# Minimal s-expression parser that keeps character offsets so the file can be
# edited in place without reformatting anything else.
# --------------------------------------------------------------------------

class Node:
    __slots__ = ("start", "end", "items")

    def __init__(self, start):
        self.start = start
        self.end = None
        self.items = []  # Node or Atom

    def head(self):
        return self.items[0].text if self.items and isinstance(self.items[0], Atom) else None

    def children(self, name):
        return [n for n in self.items if isinstance(n, Node) and n.head() == name]


class Atom:
    __slots__ = ("text", "start", "end", "quoted")

    def __init__(self, text, start, end, quoted):
        self.text, self.start, self.end, self.quoted = text, start, end, quoted


def parse_sexpr(src):
    stack, root = [], None
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "(":
            node = Node(i)
            if stack:
                stack[-1].items.append(node)
            stack.append(node)
            i += 1
        elif c == ")":
            node = stack.pop()
            node.end = i + 1
            if not stack:
                root = node
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and src[j] != '"':
                if src[j] == "\\" and j + 1 < n:
                    nxt = src[j + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                    j += 2
                else:
                    buf.append(src[j])
                    j += 1
            stack[-1].items.append(Atom("".join(buf), i, j + 1, True))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not src[j].isspace() and src[j] not in '()"':
                j += 1
            stack[-1].items.append(Atom(src[i:j], i, j, False))
            i = j
    if root is None or stack:
        raise ValueError("unbalanced parentheses")
    return root


def quote(s):
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"' + s + '"'


def line_indent(src, pos):
    line_start = src.rfind("\n", 0, pos) + 1
    seg = src[line_start:pos]
    return seg if seg.strip() == "" else "\t\t"


def property_block(name, value, indent, nl, version):
    # KiCad 10 files (format 2025xxxx) put "(hide yes)" at property level next to
    # show_name; KiCad 8/9 files keep it inside effects. Match the file's format.
    if version >= 20250000:
        lines = [
            f"(property {quote(name)} {quote(value)}",
            "\t(at 0 0 0)",
            "\t(show_name no)",
            "\t(do_not_autoplace no)",
            "\t(hide yes)",
            "\t(effects",
            "\t\t(font",
            "\t\t\t(size 1.27 1.27)",
            "\t\t)",
            "\t)",
            ")",
        ]
    else:
        lines = [
            f"(property {quote(name)} {quote(value)}",
            "\t(at 0 0 0)",
            "\t(effects",
            "\t\t(font",
            "\t\t\t(size 1.27 1.27)",
            "\t\t)",
            "\t\t(hide yes)",
            "\t)",
            ")",
        ]
    return nl.join(indent + l for l in lines)


# --------------------------------------------------------------------------
# Supplier searches
# --------------------------------------------------------------------------

def norm(pn):
    return "".join(pn.upper().split())


def http_json(url, data=None, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def brand_matches(brand, mfr):
    if not mfr or not brand:
        return False
    b, m = brand.lower(), mfr.lower()
    first = m.split()[0] if m.split() else m
    return first in b or b.split()[0] in m


class LcscSearch:
    URL = ("https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/"
           "smtGood/selectSmtComponentList")
    name = "LCSC"

    def available(self):
        return True

    def search(self, mpn, mfr):
        d = http_json(self.URL, {"keyword": mpn, "currentPage": 1, "pageSize": 25})
        lst = (((d.get("data") or {}).get("componentPageInfo") or {}).get("list")) or []
        target = norm(mpn)
        hits = []
        for p in lst:
            code = p.get("componentCode") or ""
            brand = p.get("componentBrandEn") or ""
            if not code.startswith("C") or "JLCPCB" in brand.upper():
                continue
            if norm(p.get("componentModelEn") or "") != target:
                continue
            hits.append((brand_matches(brand, mfr), p.get("stockCount") or 0, code))
        if not hits:
            return None
        hits.sort(reverse=True)
        code = hits[0][2]
        return code, f"https://www.lcsc.com/product-detail/{code}.html"


class DigikeySearch:
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"
    name = "DigiKey"

    def __init__(self):
        self.cid = os.environ.get("DIGIKEY_CLIENT_ID", "").strip()
        self.secret = os.environ.get("DIGIKEY_CLIENT_SECRET", "").strip()
        self.site = os.environ.get("DIGIKEY_LOCALE_SITE", "US").strip() or "US"
        self.currency = os.environ.get("DIGIKEY_CURRENCY", "USD").strip() or "USD"
        self.token = None
        self.token_exp = 0

    def available(self):
        return bool(self.cid and self.secret)

    def _token(self):
        if self.token and time.time() < self.token_exp - 60:
            return self.token
        body = urllib.parse.urlencode({
            "client_id": self.cid, "client_secret": self.secret,
            "grant_type": "client_credentials"}).encode()
        d = http_json(self.TOKEN_URL, body,
                      {"Content-Type": "application/x-www-form-urlencoded"})
        self.token = d["access_token"]
        self.token_exp = time.time() + int(d.get("expires_in", 600))
        return self.token

    def search(self, mpn, mfr):
        headers = {
            "Authorization": "Bearer " + self._token(),
            "X-DIGIKEY-Client-Id": self.cid,
            "X-DIGIKEY-Locale-Site": self.site,
            "X-DIGIKEY-Locale-Language": "en",
            "X-DIGIKEY-Locale-Currency": self.currency,
        }
        d = http_json(self.SEARCH_URL, {"Keywords": mpn, "Limit": 25, "Offset": 0}, headers)
        products = (d.get("ExactMatches") or []) + (d.get("Products") or [])
        target = norm(mpn)
        hits = []
        for p in products:
            if norm(p.get("ManufacturerProductNumber") or "") != target:
                continue
            brand = (p.get("Manufacturer") or {}).get("Name") or ""
            variations = p.get("ProductVariations") or []
            # Prefer Cut Tape, then anything with a part number.
            pick = None
            for v in variations:
                pkg = ((v.get("PackageType") or {}).get("Name") or "").lower()
                if "cut tape" in pkg and v.get("DigiKeyProductNumber"):
                    pick = v
                    break
            if pick is None:
                pick = next((v for v in variations if v.get("DigiKeyProductNumber")), None)
            if pick is None:
                continue
            hits.append((brand_matches(brand, mfr), p.get("QuantityAvailable") or 0,
                         pick["DigiKeyProductNumber"], p.get("ProductUrl") or ""))
        if not hits:
            return None
        hits.sort(reverse=True)
        _, _, dk_pn, url = hits[0]
        return dk_pn, url


# --------------------------------------------------------------------------
# Main processing
# --------------------------------------------------------------------------

def get_props(sym):
    props = {}
    for p in sym.children("property"):
        if len(p.items) >= 3 and isinstance(p.items[1], Atom) and isinstance(p.items[2], Atom):
            props[p.items[1].text] = p
    return props


def first_value(props, keys):
    for k in keys:
        p = props.get(k)
        if p is not None and p.items[2].text.strip() not in BAD_VALUES:
            return p.items[2].text.strip()
    return ""


def process_file(path, searchers, args, cache):
    with open(path, "r", encoding="utf-8", newline="") as f:
        src = f.read()
    nl = "\r\n" if "\r\n" in src else "\n"
    root = parse_sexpr(src)
    if root.head() != "kicad_symbol_lib":
        print(f"  skipped: not a symbol library")
        return False
    version = 0
    for v in root.children("version"):
        try:
            version = int(v.items[1].text)
        except (IndexError, ValueError):
            pass

    edits = []  # (start, end, replacement)
    symbols = root.children("symbol")
    if not symbols:
        print("  (no symbols)")
    for sym in symbols:
        sym_name = sym.items[1].text if len(sym.items) > 1 else "?"
        props = get_props(sym)
        mpn = first_value(props, MPN_KEYS) or sym_name
        mfr = first_value(props, MFR_KEYS)

        found = {}
        log = []
        for key, searcher in searchers.items():
            pn_field, link_field = VENDOR_FIELDS[key]
            need = args.force or any(
                f not in props or not props[f].items[2].text.strip()
                for f in (pn_field, link_field))
            if not need:
                log.append(f"{searcher.name}: kept existing")
                continue
            if args.no_search:
                continue
            if not searcher.available():
                log.append(f"{searcher.name}: no API key, left empty")
                continue
            ck = (key, norm(mpn), (mfr or "").lower())
            if ck not in cache:
                try:
                    cache[ck] = searcher.search(mpn, mfr)
                except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                    log.append(f"{searcher.name}: search error ({e})")
                    cache[ck] = "error"
                time.sleep(args.delay)
            res = cache[ck]
            if res and res != "error":
                found[pn_field], found[link_field] = res
                log.append(f"{searcher.name}: {res[0]}")
            elif res is None:
                log.append(f"{searcher.name}: no exact match")

        # Update existing fields, collect missing ones.
        missing = []
        for field in FIELDS:
            new_val = found.get(field)
            p = props.get(field)
            if p is None:
                missing.append((field, new_val or ""))
                continue
            cur = p.items[2].text
            if new_val and (not cur.strip() or (args.force and cur != new_val)):
                a = p.items[2]
                edits.append((a.start, a.end, quote(new_val)))

        if missing:
            existing = sym.children("property")
            if existing:
                anchor = existing[-1]
                indent = line_indent(src, anchor.start)
                pos = anchor.end
            else:
                name_atom = sym.items[1]
                indent = line_indent(src, sym.start) + "\t"
                pos = name_atom.end
            text = "".join(nl + property_block(f, v, indent, nl, version) for f, v in missing)
            edits.append((pos, pos, text))
            log.append("added " + ", ".join(f for f, _ in missing))

        print(f"  {sym_name}  [PN: {mpn}]  " + ("; ".join(log) or "nothing to do"))

    if not edits:
        return False
    out = src
    for start, end, rep in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + rep + out[end:]
    parse_sexpr(out)  # sanity check: still balanced
    if args.dry_run:
        return True
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    return True


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default_glob = os.path.join(os.path.dirname(here), "symbols", "*.kicad_sym")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*",
                    help="Symbol library files or globs (default: symbols/*.kicad_sym)")
    ap.add_argument("--no-search", action="store_true",
                    help="Only add empty fields; don't search suppliers")
    ap.add_argument("--force", action="store_true",
                    help="Re-search and overwrite existing non-empty values")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Seconds to wait between supplier requests (default 0.5)")
    args = ap.parse_args()

    files = []
    for p in (args.paths or [default_glob]):
        matches = glob.glob(p)
        files.extend(matches if matches else [p])
    files = sorted(set(f for f in files if f.endswith(".kicad_sym")))
    if not files:
        print("No .kicad_sym files found.")
        return 1

    searchers = {"digikey": DigikeySearch(), "lcsc": LcscSearch()}
    if not args.no_search and not searchers["digikey"].available():
        print("Note: DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET not set - "
              "DigiKey fields will be added empty.\n")

    cache, changed = {}, 0
    for path in files:
        print(os.path.basename(path))
        try:
            if process_file(path, searchers, args, cache):
                changed += 1
        except ValueError as e:
            print(f"  ERROR parsing file: {e}")
    print(f"\n{changed} file(s) {'would be ' if args.dry_run else ''}updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
