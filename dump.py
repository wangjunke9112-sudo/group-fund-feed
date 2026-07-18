import urllib.request
url = "https://www.gz.gov.cn/xw/tzgg/"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
print("TOTAL LENGTH:", len(html))
i = html.find("post_")
print("=== 1200 chars around first 'post_' link ===")
print(html[i-600:i+600] if i > 0 else "NO 'post_' FOUND — dumping head:\n" + html[:1500])
