"""Small public Blackcat access probe; it does not establish platform latency."""

import hashlib
import re
from datetime import datetime, timezone

import httpx


HOMEPAGE = "https://tousu.sina.com.cn/"
DETAIL_PREFIX = "https://tousu.sina.com.cn/complaint/view/"


class ProbeError(RuntimeError):
    pass


def _get(url):
    started = datetime.now(timezone.utc)
    try:
        response = httpx.get(url, timeout=15, follow_redirects=False,
                             headers={"User-Agent": "RiskShieldDay2/0.1 public-source-verification"})
    except httpx.HTTPError:
        raise ProbeError("黑猫公开页面请求失败。") from None
    finished = datetime.now(timezone.utc)
    if response.status_code != 200 or len(response.content) > 250_000:
        raise ProbeError(f"黑猫公开页面返回 HTTP {response.status_code} 或超出大小限制。")
    return response, started, finished


def probe_blackcat_homepage():
    homepage, started, listed = _get(HOMEPAGE)
    text = homepage.text
    if 'class="seo_data_list"' not in text:
        raise ProbeError("首页缺少公开投诉列表，不能作为采集成功。")
    items = re.findall(r'href="//tousu\.sina\.com\.cn/complaint/view/(\d{8,14})"', text)
    unique_ids = list(dict.fromkeys(items))
    if not unique_ids:
        raise ProbeError("首页没有可核对的投诉详情链接。")
    complaint_id = unique_ids[0]
    detail, detail_started, collected = _get(DETAIL_PREFIX + complaint_id)
    detail_verified = all(marker in detail.text for marker in
                          ("投诉编号", "投诉进度", complaint_id))
    return {
        "probe_kind": "public_homepage_to_first_detail",
        "homepage_url": HOMEPAGE,
        "detail_url": DETAIL_PREFIX + complaint_id,
        "complaint_id": complaint_id,
        "listing_started_at": started.isoformat(),
        "listing_observed_at": listed.isoformat(),
        "detail_started_at": detail_started.isoformat(),
        "detail_collected_at": collected.isoformat(),
        "homepage_http_status": homepage.status_code,
        "detail_http_status": detail.status_code,
        "homepage_response_sha256": hashlib.sha256(homepage.content).hexdigest(),
        "detail_response_sha256": hashlib.sha256(detail.content).hexdigest(),
        "detail_content_verified": detail_verified,
        "homepage_listed_unique_ids": len(unique_ids),
        "homepage_request_ms": round((listed - started).total_seconds() * 1000),
        "detail_request_ms": round((collected - detail_started).total_seconds() * 1000),
        "observed_list_to_detail_seconds": (
            round((collected - listed).total_seconds(), 3) if detail_verified else None),
        "platform_first_public_at": None,
        "platform_to_discovery_latency_seconds": None,
        "insurance_event_verified": False,
        "coverage": "首页可见 SEO 列表的首条详情，非商家全量或保险样本批次",
    }
