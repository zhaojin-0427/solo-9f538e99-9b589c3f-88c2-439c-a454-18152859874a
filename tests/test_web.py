# -*- coding: utf-8 -*-
"""Flask 路由层测试。"""

import io
import os

import pytest

from app import create_app
from app.db import init_db

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples")


def _sample(name):
    with open(os.path.join(SAMPLE_DIR, name), "rb") as fh:
        return fh.read()


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "web.db")
    init_db(db_path)
    os.environ["SCHED_DB_PATH"] = db_path
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def _upload(c, itinerary_no, rev, raw):
    return c.post(
        "/upload",
        data={
            "itinerary_no": itinerary_no,
            "revision_no": str(rev),
            "schedule_csv": (io.BytesIO(raw), "sched.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _upload_location(c, itinerary_no, rev, raw):
    """上传但不跟随跳转，返回完成页 Location（携带该次提交的 submission ID）。"""
    resp = c.post(
        "/upload",
        data={
            "itinerary_no": itinerary_no,
            "revision_no": str(rev),
            "schedule_csv": (io.BytesIO(raw), "sched.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    return resp.headers["Location"]


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "联程航班".encode() in resp.data


def test_upload_publish_and_view(client):
    resp = _upload(client, "IT-W1", 1, _sample("sample1_valid_ambiguous_unique_R1.csv"))
    assert resp.status_code == 200
    assert "已发布".encode() in resp.data
    assert "UTC".encode() in resp.data
    # 详情页
    detail = client.get("/itinerary/IT-W1")
    assert detail.status_code == 200
    assert b"R1" in detail.data


def test_rejected_upload_shows_reasons(client):
    resp = _upload(client, "IT-W2", 1, _sample("sample3_nonexistent_time_R1.csv"))
    assert "不存在".encode() in resp.data
    # 没有发布版本 -> 下载 404
    dl = client.get("/download/IT-W2")
    assert dl.status_code == 404


def test_download_published_csv(client):
    _upload(client, "IT-W3", 1, _sample("sample4_valid_3legs_R1.csv"))
    dl = client.get("/download/IT-W3")
    assert dl.status_code == 200
    assert dl.headers["Content-Disposition"].startswith("attachment;")
    text = dl.data.decode()
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    assert len(lines) == 4  # 表头 + 3 段
    assert "depart_utc" in lines[0]
    # 历史未发布修订不可下载
    _upload(client, "IT-W3", 2, _sample("sample2_two_interpretations_R1.csv"))
    assert client.get("/download/IT-W3?revision=2").status_code == 404
    # 当前版仍可下载且不受拒绝影响
    assert client.get("/download/IT-W3").status_code == 200


def test_conflict_flow_via_http(client):
    _upload(client, "IT-W4", 1, _sample("sample1_valid_ambiguous_unique_R1.csv"))
    resp = _upload(client, "IT-W4", 1, _sample("sample4_valid_3legs_R1.csv"))
    assert "冲突".encode() in resp.data


def test_missing_file_returns_flash(client):
    resp = client.post(
        "/upload",
        data={"itinerary_no": "IT-W5", "revision_no": "1"},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "选择要上传".encode() in resp.data


def test_malformed_csv_for_new_itinerary_redirects_home(client):
    # 行程尚不存在且 CSV 结构性错误 -> 回首页（而非不存在的详情页），并显示原因
    resp = client.post(
        "/upload",
        data={
            "itinerary_no": "IT-W6",
            "revision_no": "1",
            "schedule_csv": (io.BytesIO(b"a,b,c\n1,2,3\n"), "bad.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert resp.request.path == "/"
    assert "缺少必需列".encode() in resp.data
    assert client.get("/itinerary/IT-W6").status_code == 404


def test_unknown_itinerary_404(client):
    assert client.get("/itinerary/NO-SUCH").status_code == 404


def _submission_card(html: bytes) -> bytes:
    """截取「本次提交」裁决卡片，避免与修订历史/当前发布区的文案混淆。"""
    marker = "本次提交".encode()
    start = html.index(marker)
    end = html.index("当前已发布的完整 UTC 时间线".encode(), start)
    return html[start:end]


def test_completion_pages_keep_own_verdict_after_conflict(client):
    # 同行程同修订号：先发布、后冲突。两个完成页各自固定，不串换。
    first_loc = _upload_location(client, "IT-C1", 1, _sample("sample1_valid_ambiguous_unique_R1.csv"))
    second_loc = _upload_location(client, "IT-C1", 1, _sample("sample4_valid_3legs_R1.csv"))
    assert "submission=" in first_loc and "submission=" in second_loc
    assert first_loc != second_loc

    first_card = _submission_card(client.get(first_loc).data)
    second_card = _submission_card(client.get(second_loc).data)
    # 先发布者的提交卡片显示本次「已发布」，且不含后到的冲突理由
    assert "已发布为 R1".encode() in first_card
    assert "冲突拒绝".encode() not in first_card
    # 后到冲突者的提交卡片固定显示冲突拒绝
    assert "冲突拒绝".encode() in second_card
    assert "已发布为 R1".encode() not in second_card
    # 刷新先发布者链接，结果依旧（裁决归属稳定）
    refreshed_card = _submission_card(client.get(first_loc).data)
    assert "已发布为 R1".encode() in refreshed_card
    assert "冲突拒绝".encode() not in refreshed_card
    # 页面另行展示查询时的当前已发布完整时间线，且未被冲突改变
    assert "当前已发布 R1".encode() in client.get(second_loc).data


def test_rejected_then_corrected_same_revision_pages_stay_distinct(client):
    # 先拒绝（多解释）、后同号修正发布：第一次完成页的提交卡片仍是拒绝原因
    reject_loc = _upload_location(client, "IT-C2", 1, _sample("sample2_two_interpretations_R1.csv"))
    publish_loc = _upload_location(client, "IT-C2", 1, _sample("sample1_valid_ambiguous_unique_R1.csv"))

    reject_card = _submission_card(client.get(reject_loc).data)
    publish_card = _submission_card(client.get(publish_loc).data)
    assert "无法唯一确定".encode() in reject_card
    assert "已发布为 R1".encode() not in reject_card
    assert "已发布为 R1".encode() in publish_card
    # 刷新拒绝页链接，拒绝原因不被后到的发布结果替换
    refreshed_card = _submission_card(client.get(reject_loc).data)
    assert "无法唯一确定".encode() in refreshed_card
    assert "已发布为 R1".encode() not in refreshed_card
    # 拒绝页同时展示查询时已存在的当前发布时间线（第二次修正已发布）
    assert "当前已发布 R1".encode() in client.get(reject_loc).data


def test_completion_url_with_unknown_submission_id_404(client):
    _upload(client, "IT-C3", 1, _sample("sample1_valid_ambiguous_unique_R1.csv"))
    resp = client.get("/itinerary/IT-C3?submission=999999")
    assert resp.status_code == 404


def test_plain_detail_page_has_no_submission_card(client):
    _upload(client, "IT-C4", 1, _sample("sample1_valid_ambiguous_unique_R1.csv"))
    resp = client.get("/itinerary/IT-C4")
    assert resp.status_code == 200
    assert "本次提交".encode() not in resp.data
    assert "当前已发布 R1".encode() in resp.data


def test_sample_download(client):
    resp = client.get("/samples/sample4_valid_3legs_R1.csv")
    assert resp.status_code == 200
    assert b"flight_minutes" in resp.data
