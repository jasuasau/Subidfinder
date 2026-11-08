
import argparse
import re
import sys
import subprocess
import urllib.parse
from collections import deque
from typing import Set, List, Tuple, Dict, Optional

import requests
from bs4 import BeautifulSoup
from colorama import Fore, Style, init as colorama_init

try:
    import pyfiglet
    HAS_FIGLET = True
except ImportError:
    HAS_FIGLET = False

colorama_init(autoreset=True)

SQL_PARAM_NAMES: Set[str] = {
    "id", "catid", "cartid", "pid", "productid", "itemid", "uid", "userid",
    "postid", "pageid", "blogid", "newsid", "articleid", "memberid", "orderid",
    "tid", "threadid", "fid", "forumid", "gid", "groupid", "aid", "authorid",
    "cid", "commentid", "sid", "sectionid", "rid", "recordid", "did", "docid",
    "vid", "videoid", "imageid", "imgid", "fileid", "typeid", "modid", "subid",
    "parent_id", "category_id", "product_id", "user_id", "post_id", "page_id",
    "item_id", "cat", "category", "prod", "product", "post", "page", "item",
    "section", "article", "thread", "news", "blog", "forum", "group", "member",
    "order", "cart", "gallery", "photo", "video", "doc", "file", "ref",
    "key", "no", "num", "record", "entry", "node", "view", "show",
}

SQL_SUFFIX_RE = re.compile(r"^.*(id|_id|num|no|code|key|ref)$", re.IGNORECASE)

def is_sql_param(name: str) -> bool:
    return name.lower() in SQL_PARAM_NAMES or bool(SQL_SUFFIX_RE.match(name))

def extract_sql_params(url: str) -> List[str]:
    """Return list of param names in the URL that look SQL-injectable."""
    try:
        parsed = urllib.parse.urlparse(url)
        if not parsed.query:
            return []
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        return [k for k in params if is_sql_param(k)]
    except Exception:
        return []

JS_URL_RE = re.compile(
    r"""[\"'`]((?:https?://[^\"'`<>\s]{4,}|/[^\"'`<>\s]{0,300})\?[^\"'`<>\s]+)[\"'`]""",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

def normalize_url(base: str, href: str) -> Optional[str]:
    try:
        resolved = urllib.parse.urljoin(base, href.strip())
        parsed = urllib.parse.urlparse(resolved)
        if parsed.scheme not in ("http", "https"):
            return None
        return resolved
    except Exception:
        return None

def same_domain(root: str, url: str) -> bool:
    def strip_www(h: str) -> str:
        return re.sub(r"^www\.", "", h.lower())
    root_host = strip_www(urllib.parse.urlparse(root).netloc)
    url_host  = strip_www(urllib.parse.urlparse(url).netloc)
    return url_host == root_host or url_host.endswith("." + root_host)

def strip_fragment(url: str) -> str:
    return urllib.parse.urldefrag(url)[0]

def dedup_url(url: str) -> str:
    try:
        p = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        qs_sorted = sorted(qs)
        return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(qs_sorted)))
    except Exception:
        return url

def scrape_page(
    session: requests.Session,
    url: str,
    root: str,
    skip_js: bool,
) -> Tuple[Set[str], Set[str]]:
    crawl_links: Set[str] = set()
    injectable:  Set[str] = set()

    try:
        resp = session.get(url, timeout=10, headers=HEADERS,
                           allow_redirects=True, stream=False)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        print(Fore.RED + f"    [!] {url} — {exc}")
        return crawl_links, injectable

    ct   = resp.headers.get("Content-Type", "").lower()
    body = resp.text

    def register(raw: str) -> None:
        full = normalize_url(url, raw)
        if not full:
            return
        if not same_domain(root, full):
            return
        clean = strip_fragment(dedup_url(full))
        crawl_links.add(clean)
        if extract_sql_params(clean):
            injectable.add(clean)

    #  HTML parse 
    if "html" in ct or re.search(r"\.(php|asp|aspx|jsp|html?|cfm)(\?|$)", url, re.I):
        try:
            soup = BeautifulSoup(body, "html.parser")

            # <a href>  <link href>  <area href>
            for tag in soup.find_all(["a", "link", "area"], href=True):
                register(tag["href"])

            # <form action> + collect all input/select names as a synthetic URL
            for form in soup.find_all("form"):
                action = form.get("action", url)
                inputs: Dict[str, str] = {}
                for inp in form.find_all(["input", "select", "textarea"]):
                    name = inp.get("name")
                    if name:
                        inputs[name] = inp.get("value", "1")
                base_action = normalize_url(url, action)
                if base_action and same_domain(root, base_action):
                    crawl_links.add(strip_fragment(base_action))
                    if inputs:
                        qs   = urllib.parse.urlencode(inputs)
                        sep  = "&" if "?" in base_action else "?"
                        full_form = base_action + sep + qs
                        if extract_sql_params(full_form):
                            injectable.add(strip_fragment(full_form))

            for tag in soup.find_all(["script", "iframe", "img", "embed", "source"]):
                src = tag.get("src")
                if src:
                    register(src)
                    # queue external JS for its own scrape pass
                    if not skip_js and tag.name == "script" and src.endswith(".js"):
                        full_js = normalize_url(url, src)
                        if full_js and same_domain(root, full_js):
                            crawl_links.add(strip_fragment(full_js))

            for tag in soup.find_all(True):
                for attr, val in tag.attrs.items():
                    if not isinstance(val, str):
                        continue
                    if (attr.startswith("data-") or attr in
                            ("hx-get","hx-post","hx-put","hx-delete",
                             "ng-href","ng-src","x-bind:href",":href")):
                        if "?" in val:
                            register(val)

            for script in soup.find_all("script"):
                js_src = script.string or ""
                for m in JS_URL_RE.finditer(js_src):
                    register(m.group(1))
                for m in re.finditer(
                    r"""fetch\s*\(\s*[\"'`]([^\"'`]+\?[^\"'`]+)[\"'`]""", js_src
                ):
                    register(m.group(1))

        except Exception as exc:
            print(Fore.YELLOW + f"    [~] parse error {url}: {exc}")

    #  External JS file 
    if not skip_js and ("javascript" in ct or url.endswith(".js")):
        for m in JS_URL_RE.finditer(body):
            register(m.group(1))
        for m in re.finditer(
            r"""fetch\s*\(\s*[\"'`]([^\"'`]+\?[^\"'`]+)[\"'`]""", body
        ):
            register(m.group(1))

    for m in re.finditer(
        r"https?://[^\s\"'<>{}|\\^`\[\]\(\)]{8,}", body
    ):
        candidate = m.group(0).rstrip(".,;):\"'>")
        if same_domain(root, candidate) and extract_sql_params(candidate):
            injectable.add(strip_fragment(dedup_url(candidate)))

    return crawl_links, injectable

def crawl(
    start: str,
    depth: int,
    session: requests.Session,
    skip_js: bool,
) -> Set[str]:
    visited:    Set[str] = set()
    injectable: Set[str] = set()
    queue: deque = deque([(start, 0)])

    print(Fore.CYAN + f"\n  [*] starting crawl — depth {depth}\n")

    while queue:
        url, level = queue.popleft()
        url = strip_fragment(dedup_url(url))

        if url in visited or level > depth:
            continue
        visited.add(url)

        depth_color = [Fore.GREEN, Fore.YELLOW, Fore.CYAN, Fore.MAGENTA, Fore.WHITE]
        dc = depth_color[min(level, len(depth_color) - 1)]
        print(dc + f"  [d{level}] " + Fore.WHITE + url)

        links, found = scrape_page(session, url, start, skip_js)
        injectable.update(found)

        for link in links:
            if link not in visited:
                queue.append((link, level + 1))

    return injectable

def run_sqlmap(url: str, param: str, extra_args: str, cookie: str, proxy: str) -> None:
    cmd = ["sqlmap", "-u", url, "-p", param, "--batch"]

    if cookie:
        cmd += ["--cookie", cookie]
    if proxy:
        cmd += ["--proxy", proxy]
    if extra_args.strip():
        cmd += extra_args.strip().split()

    print(Fore.YELLOW + "\n  >> " + " ".join(cmd) + "\n")
    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        print(Fore.RED + "  [!] sqlmap not in PATH — install it or add it")
    except KeyboardInterrupt:
        print(Fore.YELLOW + "\n  [~] sqlmap interrupted")

def print_banner() -> None:
    if HAS_FIGLET:
        txt = pyfiglet.figlet_format("SubID-Finder", font="slant")
        print(Fore.RED + txt)
    print()

# MAIN

def main() -> None:
    print_banner()

    parser = argparse.ArgumentParser(
        prog="subidfinder",
        description="Crawl a site, harvest injectable params, launch sqlmap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python subidfinder.py http://target.com -d 3\n"
            "  python subidfinder.py http://target.com -d 2 --cookie 'PHPSESSID=abc123'\n"
            "  python subidfinder.py http://target.com --proxy http://127.0.0.1:8080\n"
        ),
    )
    parser.add_argument("url",          nargs="?",             help="Target URL (prompted if omitted)")
    parser.add_argument("-d","--depth", type=int, default=3,   help="Crawl depth (default: 3)")
    parser.add_argument("--no-js",      action="store_true",   help="Skip external .js file parsing")
    parser.add_argument("--list-only",  action="store_true",   help="Print found params, skip sqlmap")
    parser.add_argument("--cookie",     default="",            help="Cookie header (e.g. 'PHPSESSID=x')")
    parser.add_argument("--proxy",      default="",            help="HTTP proxy (e.g. http://127.0.0.1:8080)")
    parser.add_argument("--timeout",    type=int, default=10,  help="Request timeout in seconds")
    args = parser.parse_args()

    #  target 
    target = args.url
    if not target:
        target = input(Fore.CYAN + "  [?] target URL: " + Style.RESET_ALL).strip()
    if not target.startswith(("http://", "https://")):
        target = "http://" + target
    target = strip_fragment(target)

    #  session ─
    session = requests.Session()
    session.headers.update(HEADERS)
    if args.cookie:
        session.headers["Cookie"] = args.cookie
    if args.proxy:
        session.proxies = {"http": args.proxy, "https": args.proxy}

    #  crawl ─
    injectable_urls = crawl(target, args.depth, session, args.no_js)

    if not injectable_urls:
        print(Fore.RED + "\n  [!] no injectable parameters found.\n")
        sys.exit(0)

    #  build entry list ─
    # entries: [ (url, param), ... ]
    entries: List[Tuple[str, str]] = []
    seen_pairs: Set[Tuple[str, str]] = set()

    for url in sorted(injectable_urls):
        for param in extract_sql_params(url):
            key = (url, param)
            if key not in seen_pairs:
                seen_pairs.add(key)
                entries.append(key)

    #  display 
    print(Fore.GREEN + f"\n  [+] {len(entries)} injectable endpoint(s) found\n")
    print(Fore.WHITE + "  " + "─" * 70)

    for i, (url, param) in enumerate(entries, 1):
        short = url if len(url) <= 90 else url[:87] + "..."
        print(
            Fore.YELLOW + f"  [{i:>3}] " +
            Fore.CYAN   + f"-p {param:<20} " +
            Fore.WHITE  + short
        )

    print(Fore.WHITE + "  " + "─" * 70)

    if args.list_only:
        sys.exit(0)

    #  sqlmap prompt loop ─
    print(
        Fore.CYAN +
        "\n  enter a number to test that entry, "
        "'all' to chain all, or 'q' to quit\n"
    )

    while True:
        try:
            choice = input(Fore.CYAN + "  >> " + Style.RESET_ALL).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice in ("q", "quit", "exit"):
            print(Fore.WHITE + "  done.")
            break

        if choice == "all":
            to_test = entries
        else:
            try:
                n = int(choice) - 1
                if n < 0 or n >= len(entries):
                    print(Fore.RED + f"  [!] range is 1–{len(entries)}")
                    continue
                to_test = [entries[n]]
            except ValueError:
                print(Fore.RED + "  [!] number, 'all', or 'q'")
                continue

        for url, param in to_test:
            print(Fore.WHITE  + f"\n  url   : {url}")
            print(Fore.YELLOW + f"  param : {param}")
            try:
                extra = input(
                    Fore.CYAN +
                    "  sqlmap args (e.g. --level=3 --risk=2 --dbs --tamper=space2comment --tor): " +
                    Style.RESET_ALL
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            run_sqlmap(url, param, extra, args.cookie, args.proxy)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(Fore.YELLOW + "\n  [~] bye\n")
        sys.exit(0)