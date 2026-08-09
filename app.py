"""JARVIS 네이버 순위 검색 API 서버.

naver_rank_v3의 파싱 로직을 웹 API로 감싼 것.
  GET /rank?q=<검색어>&top=20   → 순위 JSON
  GET /health                   → 상태 확인

환경변수:
  API_KEY          설정하면 요청에 ?key=<값> 또는 X-Api-Key 헤더 필요 (권장)
  ALLOWED_ORIGINS  CORS 허용 도메인, 콤마 구분 (기본 * — JARVIS 도메인으로 좁히기 권장)
  CACHE_TTL        같은 키워드 캐시 유지 초 (기본 600 = 10분, 과요청 차단 방지)
"""
import datetime, json, os, re, threading, time
from urllib.parse import quote

from curl_cffi import requests as cffi_requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

API_KEY = os.environ.get("API_KEY", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "600"))
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")]

app = FastAPI(title="JARVIS naver-rank API")
app.add_middleware(
    CORSMiddleware, allow_origins=ORIGINS,
    allow_methods=["GET"], allow_headers=["*"],
)

_cache = {}           # keyword -> (timestamp, payload)
_lock = threading.Lock()
_last_fetch = [0.0]   # 네이버 요청 간 최소 간격용


# ---------- naver_rank_v3 파싱 로직 (동일) ----------

def _blobs(h, marker="_INITIAL_STATE"):
    out = []
    for m in re.finditer(re.escape(marker) + r"\s*=\s*\{", h):
        start = m.end() - 1
        depth, i, instr, esc = 0, start, False, False
        while i < len(h):
            c = h[i]
            if instr:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': instr = False
            else:
                if c == '"': instr = True
                elif c == "{": depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        out.append(h[start:i + 1]); break
            i += 1
    return out


def _sanitize(t):
    t = re.sub(r"(?<=[:\[,])\s*undefined\s*(?=[,}\]])", "null", t)
    t = re.sub(r"new Date\((\"[^\"]*\"|\d+)\)", r"\1", t)
    t = re.sub(r"(?<=[:\[,])\s*NaN\s*(?=[,}\]])", "null", t)
    return t


_clean = lambda x: re.sub(r"</?mark>", "", x or "")


def _row(p, pos, now):
    st = p.get("sourceType")
    kind = {"AD": "광고", "SAS": "오가닉", "SUPER_POINT": "포인트"}.get(st, st or "?")
    return {
        "screenPos": pos,
        "kind": kind,
        "rank": p.get("rank") if st == "SAS" else None,
        "name": _clean(p.get("productName")),
        "price": p.get("price") or p.get("discountedSalePrice") or p.get("salePrice"),
        "mall": p.get("mallName") or "",
        "type": "가격비교" if p.get("cardType") == "CATALOG_CARD" else "단독몰",
        "review": p.get("totalReviewCount"),
        "score": p.get("averageReviewScore"),
        "purchase": p.get("purchaseCount") or None,
        "keep": p.get("keepCount"),
        "mallCount": p.get("mallCount") or None,
        "nvMid": p.get("nvMid"),
        "ts": now,
    }


def parse_html(h):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    screen = []
    for b in _blobs(h):
        try:
            data = json.loads(_sanitize(b))
        except Exception:
            continue
        paged = (data.get("initProps") or {}).get("pagedSlot")
        if not paged:
            continue
        pos = 0
        for pg in paged:
            for sl in (pg.get("slots") or []):
                d = (sl.get("data") or {}) if isinstance(sl, dict) else {}
                if "productName" in d:
                    pos += 1
                    screen.append(_row(d, pos, now))
        if screen:
            break
    if not screen:  # pagedSlot 스키마가 바뀐 경우 폴백
        items, seen = [], set()
        def walk(o):
            if isinstance(o, dict):
                if "productName" in o:
                    k = o.get("nvMid") or o.get("productName")
                    if k not in seen:
                        seen.add(k); items.append(o)
                for v in o.values(): walk(v)
            elif isinstance(o, list):
                for v in o: walk(v)
        for b in _blobs(h):
            try: walk(json.loads(_sanitize(b)))
            except Exception: pass
        screen = [_row(p, i + 1, now) for i, p in enumerate(items)]
    return screen


def fetch_naver(kw):
    """네이버 통합검색 쇼핑 모듈 HTML을 가져온다. 디버그: DEBUG_RAW_FILE 환경변수."""
    debug = os.environ.get("DEBUG_RAW_FILE")
    if debug:
        return open(debug, encoding="utf-8").read()

    # 네이버 요청 간 최소 3초 간격 (차단 예방)
    with _lock:
        wait = 3 - (time.time() - _last_fetch[0])
        if wait > 0:
            time.sleep(wait)
        _last_fetch[0] = time.time()

    s = cffi_requests.Session(impersonate="chrome_android")
    s.headers.update({"Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"})
    s.get("https://www.naver.com/", timeout=15, referer="https://www.google.com/")
    url = f"https://search.naver.com/search.naver?where=shop&query={quote(kw)}"
    r = s.get(url, timeout=25, headers={"Referer": "https://www.naver.com/"})
    if "nid.naver.com" in str(r.url) or len(r.text) < 50000:
        raise HTTPException(status_code=502, detail={
            "error": "blocked_or_thin",
            "msg": "네이버가 요청을 차단했거나 빈 응답 (클라우드 IP 차단 가능성)",
            "http": r.status_code, "len": len(r.text),
        })
    return r.text


# ---------- API ----------

@app.get("/health")
def health():
    return {"ok": True, "cached_keywords": len(_cache), "ttl": CACHE_TTL}


@app.get("/rank")
def rank(q: str, request: Request, top: int = 20, screen: bool = False, key: str = ""):
    if API_KEY and key != API_KEY and request.headers.get("x-api-key") != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")
    kw = q.strip()
    if not kw:
        raise HTTPException(status_code=400, detail="q required")

    hit = _cache.get(kw)
    if hit and time.time() - hit[0] < CACHE_TTL:
        rows, cached = hit[1], True
    else:
        rows = parse_html(fetch_naver(kw))
        if rows:
            _cache[kw] = (time.time(), rows)
        cached = False

    organic = sorted([r for r in rows if r["kind"] == "오가닉" and r["rank"]],
                     key=lambda r: r["rank"])
    resp = {
        "keyword": kw, "cached": cached,
        "organicTotal": len(organic),
        "adTotal": sum(1 for r in rows if r["kind"] == "광고"),
        "organic": organic[:top],
    }
    if screen:
        resp["screen"] = rows
    return resp
