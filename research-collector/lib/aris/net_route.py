"""独立境外语料模块的网络出口。

公司服务器二选一（由 etc/collector.env 或进程环境注入）：

* `ARIS_PROXY_URL=http://proxy.example:8080`：境外请求经公司 HTTP(S) 代理；
* `ARIS_OVERSEAS_DIRECT=1`：公司出口已能访问境外公开站，允许直连。

两者都不配置时，真正的境外请求响亮报错；离线抽取、建库和 `--help` 不受影响。
`urllib`/`requests` 均显式构造路由，不继承 shell 中可能残留的代理变量。
不携带原服务器的 loopback 代理、告警接收人或 OpenClaw 配置。
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

# 无个人或原服务器默认值。新服务器必须显式选择代理或已验证的直连出口。
PROXY_URL = os.environ.get("ARIS_PROXY_URL", "").strip()
_OVERSEAS_DIRECT = os.environ.get("ARIS_OVERSEAS_DIRECT", "0").strip().lower() in {"1", "true", "yes"}


class RouteConfigError(RuntimeError):
    """路由未配置；采集器应立即记失败，不做无意义的网络重试。"""


def _require_route() -> None:
    # 不在 import 时失败，让 --help、--stats、离线建库无需代理配置。
    # 真正发起境外请求前必须响亮失败，不能静默直连。
    if _OVERSEAS_DIRECT and PROXY_URL:
        raise RouteConfigError(
            "ARIS_PROXY_URL 与 ARIS_OVERSEAS_DIRECT 只能二选一，请移除其中一个")
    if not _OVERSEAS_DIRECT and not PROXY_URL:
        raise RouteConfigError(
            "境外访问尚未配置：请设置 ARIS_PROXY_URL=http://公司代理:端口；"
            "若服务器可直接访问境外公开站，则设置 ARIS_OVERSEAS_DIRECT=1")

# ---------------------------------------------------------------------------
# 🔴 308 Permanent Redirect：Python < 3.11 的 stdlib 缺口（2026-08-25 实测）
# ---------------------------------------------------------------------------
# `urllib.request.HTTPRedirectHandler` 只实现了 `http_error_301/302/303/307`，
# **没有 308** —— 于是 308 一路冒泡成 `HTTPError(308)`，被调用方当成
# 「4xx 级永久失败」。CPython 3.11 才补上；本机是 3.10.12。
#
# 现场：Google DeepMind 迁站到 blog.google，12 篇文章全回 308：
#   deepmind.google/blog/gemini-3-flash-…
#     → blog.google/products-and-platforms/products/gemini/gemini-3-flash/
# 表现是**每小时 12 条失败 + 水位线永不推进 + 白烧 210s/轮**，已连续 13 轮
# （state.history 里 fetched:0 / failed:12 一模一样地重复）。
#
# ⚠️ 为什么修在这一层而不是采集器里：
#    这是**协议实现缺口**，不是某站的怪癖。任何走出网层的脚本都会踩。
#    在 web_rss_sites.py 里写 `if e.code == 308: 手动重试` 等于
#    「只给发现它的那一处上锁」—— 同族于 08-24 那条教训
#    （工具锁住了 workspace，而烧流量的代码在另一个 repo）。
#
# ⭕ 语义与 301 一致（永久重定向），唯一差别：**不许把 POST 降级成 GET**。
#    父类 `redirect_request()` 对 POST+301 恰恰会降级 ⇒ 对 308 是错的，
#    所以非 GET/HEAD 一律不自动跟随，保持抛错（响亮失败优于静默改语义）。
class _Redirect308Handler(urllib.request.HTTPRedirectHandler):
    """给 urllib 补上 308 支持（GET/HEAD 跟随，其他方法不跟随）。"""

    def http_error_308(self, req, fp, code, msg, headers):
        if req.get_method() not in ("GET", "HEAD"):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        return self.http_error_301(req, fp, 301, msg, headers)


# ---------------------------------------------------------------------------
# 直连 opener：空 dict 的 ProxyHandler = 禁用代理（官方语义）
# 🔴 不要写成 build_opener() —— 那会带上默认 ProxyHandler，
#    而默认 ProxyHandler 读 getproxies() ⇒ 继承环境变量 ⇒ 破功。
# ---------------------------------------------------------------------------
_DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _Redirect308Handler()
)

# 出国 opener：写死代理地址，不读环境（环境可能没配，也可能配错）
_OVERSEAS_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}),
    _Redirect308Handler(),
)


def direct_opener() -> urllib.request.OpenerDirector:
    """返回强制直连的 opener（境内一律用这个）。"""
    return _DIRECT_OPENER


def overseas_opener() -> urllib.request.OpenerDirector:
    """境外 opener：显式代理，或经批准的境外直连出口。"""
    _require_route()
    return _DIRECT_OPENER if _OVERSEAS_DIRECT else _OVERSEAS_OPENER


def direct_urlopen(req, timeout: float = 60):
    """强制直连的 urlopen。境内目标一律走这个。

    用法与 `urllib.request.urlopen` 完全一致（返回值可作 context manager）。
    """
    return _DIRECT_OPENER.open(req, timeout=timeout)


def overseas_urlopen(req, timeout: float = 60):
    """按配置访问境外公开资源；失败交给调用方重试并记状态。"""
    return overseas_opener().open(req, timeout=timeout)


# ---------------------------------------------------------------------------
# requests 版本（POST JSON 之类用 urllib 拼起来太啰嗦时用）
#
# 🔴 `trust_env=False` 是核心，不是可选优化：
#    只写 `s.proxies = {}` **不够** —— requests 仍会把环境变量合并进来
#    （workspace/lib/net_helper.py 有实测记录，tests/net_routing.py A3 断言守着）。
#
# ⚠️ requests 按需 import：A-RIS 不少脚本只用 urllib，
#    在模块顶层 import requests 会让它们多付一个依赖。
# ---------------------------------------------------------------------------
def cn_session():
    """境内用的 requests.Session（强制直连，忽略环境代理）。"""
    import requests

    s = requests.Session()
    s.trust_env = False          # 🔴 关掉环境变量继承
    s.proxies = {}
    return s


def overseas_session():
    """境外用的 requests.Session（写死代理，不读环境）。"""
    import requests

    _require_route()
    s = requests.Session()
    s.trust_env = False
    s.proxies = {} if _OVERSEAS_DIRECT else {"http": PROXY_URL, "https": PROXY_URL}
    return s


if __name__ == "__main__":
    import json
    import sys

    print("境外语料模块出网配置（不显示代理凭据）")
    print(f"  模式 = {'approved_direct' if _OVERSEAS_DIRECT else ('proxy' if PROXY_URL else 'UNCONFIGURED')}")
    if len(sys.argv) > 1:
        _require_route()
        import time
        for url in sys.argv[1:]:
            t0 = time.time()
            try:
                with overseas_opener().open(url, timeout=20) as r:
                    print(f"  {url} → HTTP {r.status} ({(time.time()-t0)*1000:.0f}ms)")
            except Exception as e:
                print(f"  {url} → {type(e).__name__}: {str(e)[:100]}")
                raise
