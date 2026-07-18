import sys, os, traceback
import urllib.request

url = "https://www.gz.gov.cn/xw/tzgg/"
os.makedirs("data", exist_ok=True)

try:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    resp = urllib.request.urlopen(req, timeout=30)
    raw = resp.read()
    print("HTTP STATUS:", resp.status, flush=True)
    print("BYTES:", len(raw), flush=True)
    print("HEADERS:", dict(resp.headers), flush=True)

    text = raw.decode("utf-8", "replace")
    with open("data/list_raw.html", "w", encoding="utf-8") as fh:
        fh.write(text)
    print("SAVED to data/list_raw.html", flush=True)

    print("post_ count:", text.count("post_"), flush=True)
    print("tzgg count:", text.count("tzgg"), flush=True)
    i = text.find("post_")
    print("=== around first post_ ===", flush=True)
    print(text[max(0, i-600): i+600] if i > 0 else text[:1500], flush=True)

except Exception:
    traceback.print_exc()
    sys.stdout.flush()
    raise
