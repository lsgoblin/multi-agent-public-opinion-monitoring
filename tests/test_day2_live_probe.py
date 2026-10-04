import httpx

from riskshield.day2_live_probe import probe_blackcat_homepage


def test_probe_records_pipeline_hop_without_claiming_platform_latency(monkeypatch):
    def fetch(url, **_kwargs):
        if url.endswith("/complaint/view/17402031375"):
            return httpx.Response(200, text="投诉编号 17402031375 投诉进度 处理中")
        return httpx.Response(200, text=(
            '<html><div class="seo_data_list">'
            '<a href="//tousu.sina.com.cn/complaint/view/17402031375">item</a>'
            '</div></html>'))

    monkeypatch.setattr(httpx, "get", fetch)
    result = probe_blackcat_homepage()
    assert result["complaint_id"] == "17402031375"
    assert result["homepage_listed_unique_ids"] == 1
    assert result["detail_content_verified"] is True
    assert result["platform_to_discovery_latency_seconds"] is None
    assert result["insurance_event_verified"] is False


def test_http_200_notice_is_not_accepted_as_complaint(monkeypatch):
    def fetch(url, **_kwargs):
        if "/complaint/view/" in url:
            return httpx.Response(200, text="notice_sina")
        return httpx.Response(200, text=(
            '<div class="seo_data_list">'
            '<a href="//tousu.sina.com.cn/complaint/view/17402031375">item</a>'
            '</div>'))

    monkeypatch.setattr(httpx, "get", fetch)
    result = probe_blackcat_homepage()
    assert result["detail_content_verified"] is False
    assert result["observed_list_to_detail_seconds"] is None
