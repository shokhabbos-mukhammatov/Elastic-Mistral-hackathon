"""Official SF flood guidance -> ES guides index; hybrid retrieval (BM25 + Mistral-embedded semantic_text, fused with RRF)."""

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

SOURCES = Path(__file__).resolve().parent.parent / "data" / "guides" / "sources.txt"


class _Text(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg", "form"}

    def __init__(self):
        super().__init__()
        self.out, self.skip, self.title = [], 0, ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "li", "h1", "h2", "h3", "h4", "br", "tr", "div"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def page_text(html: str) -> tuple[str, str]:
    p = _Text()
    p.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    lines = [ln.strip() for ln in text.split("\n") if len(ln.strip()) > 2]
    return p.title.strip(), "\n".join(lines)


def chunks(text: str, size: int = 1200) -> list[str]:
    out, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) > size and cur:
            out.append(cur.strip())
            cur = ""
        cur += para + "\n"
    if cur.strip():
        out.append(cur.strip())
    return [c for c in out if len(c) > 80]


def load(s, http) -> int:
    docs = []
    for line in SOURCES.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        lang, url = line.split(maxsplit=1)
        r = http.get(url, headers={"User-Agent": "Mozilla/5.0 HighGround-SF"}, follow_redirects=True, timeout=30)
        if r.status_code != 200:
            continue
        title, text = page_text(r.text)
        for i, c in enumerate(chunks(text)):
            docs.append({"_id": hashlib.sha1(f"{url}#{i}".encode()).hexdigest(), "url": url, "lang": lang, "title": title, "body": c, "content": c})
    return s.write("guides", docs, refresh=True)


def semantic(s) -> bool:
    m = s.es.indices.get_mapping(index=s.ix("guides"))
    return next(iter(m.values()))["mappings"]["properties"]["content"]["type"] == "semantic_text"


def search(s, q: str, k: int = 4) -> list[dict]:
    bm25 = {"standard": {"query": {"multi_match": {"query": q, "fields": ["title^2", "body"]}}}}
    if semantic(s):  # Mistral embeddings computed inside Elasticsearch via the inference endpoint
        body = {"retriever": {"rrf": {"retrievers": [bm25, {"standard": {"query": {"semantic": {"field": "content", "query": q}}}}], "rank_window_size": 20}}}
    else:
        body = {"query": bm25["standard"]["query"]}
    r = s.search("guides", size=k, _source=["url", "title", "lang", "body"], **body)
    return [{"url": h["_source"]["url"], "title": h["_source"]["title"], "lang": h["_source"]["lang"], "text": h["_source"]["body"][:900]} for h in r["hits"]["hits"]]
