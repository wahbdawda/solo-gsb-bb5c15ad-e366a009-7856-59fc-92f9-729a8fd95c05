import random
from concurrent.futures import ThreadPoolExecutor

from app import service


def submit(client, channel, seq, revision, text="t"):
    resp = client.post(
        f"/channels/{channel}/fragments",
        json={"seq": seq, "revision": revision, "text": text},
    )
    assert resp.status_code == 200
    return resp.json()


def decide_gap(client, channel, start_seq, end_seq):
    resp = client.post(
        f"/channels/{channel}/gaps",
        json={"start_seq": start_seq, "end_seq": end_seq},
    )
    assert resp.status_code == 200
    return resp.json()


def test_in_order_submission_finalizes_immediately(client):
    body = submit(client, "c1", 1, 1, "hello")
    assert body["applied"] is True
    assert body["watermark"] == 1
    assert [i["seq"] for i in body["finalized"]] == [1]
    assert body["finalized"][0]["text"] == "hello"


def test_out_of_order_waits_for_gap_then_releases_batch(client):
    body = submit(client, "c1", 2, 1, "second")
    assert body["applied"] is True
    assert body["watermark"] == 0
    assert body["finalized"] == []

    body = submit(client, "c1", 1, 1, "first")
    assert body["watermark"] == 2
    assert [i["seq"] for i in body["finalized"]] == [1, 2]
    assert [i["text"] for i in body["finalized"]] == ["first", "second"]


def test_gap_blocks_prefix_and_fill_releases_all(client):
    assert submit(client, "c1", 1, 1)["watermark"] == 1
    assert submit(client, "c1", 2, 1)["watermark"] == 2
    body = submit(client, "c1", 4, 1)
    assert body["watermark"] == 2  # 缺口 3 未补齐，不能越过
    assert body["finalized"] == []

    body = submit(client, "c1", 3, 1)
    assert body["watermark"] == 4
    assert [i["seq"] for i in body["finalized"]] == [3, 4]


def test_higher_revision_replaces_pending_fragment(client):
    submit(client, "c1", 2, 1, "draft")
    body = submit(client, "c1", 2, 5, "revised")
    assert body["applied"] is True

    body = submit(client, "c1", 1, 1, "first")
    assert body["watermark"] == 2
    assert body["finalized"][1]["revision"] == 5
    assert body["finalized"][1]["text"] == "revised"


def test_stale_or_equal_revision_rejected(client):
    submit(client, "c1", 2, 5, "v5")
    for rev in (5, 3, 0):
        body = submit(client, "c1", 2, rev, "stale")
        assert body["applied"] is False
        assert body["reason"] == "stale_revision"
        assert body["watermark"] == 0


def test_finalized_seq_is_immutable(client):
    submit(client, "c1", 1, 1, "final")
    body = submit(client, "c1", 1, 99, "too late")
    assert body["applied"] is False
    assert body["reason"] == "already_finalized"
    assert body["watermark"] == 1

    resp = client.get("/channels/c1/finalized").json()
    assert resp["items"][0]["text"] == "final"


def test_get_finalized_with_incremental_after(client):
    for seq in (1, 2, 3):
        submit(client, "c1", seq, 1, f"line{seq}")

    resp = client.get("/channels/c1/finalized").json()
    assert resp["watermark"] == 3
    assert [i["seq"] for i in resp["items"]] == [1, 2, 3]

    resp = client.get("/channels/c1/finalized", params={"after": 1}).json()
    assert [i["seq"] for i in resp["items"]] == [2, 3]

    resp = client.get("/channels/unknown/finalized").json()
    assert resp["watermark"] == 0
    assert resp["items"] == []


def test_channels_are_isolated(client):
    submit(client, "a", 2, 1)
    assert submit(client, "b", 1, 1)["watermark"] == 1
    assert submit(client, "a", 2, 1)["watermark"] == 0  # a 的缺口仍在


def test_validation_rejects_bad_input(client):
    resp = client.post("/channels/c1/fragments", json={"seq": 0, "revision": 1, "text": "x"})
    assert resp.status_code == 422
    resp = client.post("/channels/c1/fragments", json={"seq": 1, "revision": -1, "text": "x"})
    assert resp.status_code == 422


def test_concurrent_out_of_order_submissions(conn):
    """多线程乱序提交 + 重复修订竞争：最终定稿必须是 1..N 的连续前缀。"""
    n = 300
    channel = "stress"
    jobs = []
    for seq in range(1, n + 1):
        jobs.append((seq, 1, f"seq{seq}-v1"))
        jobs.append((seq, 2, f"seq{seq}-v2"))  # 更高修订号竞争同一片段
    random.Random(42).shuffle(jobs)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda j: service.submit_fragment(channel, *j), jobs))

    assert all(r["watermark"] <= n for r in results)
    assert service.get_finalized(channel)["watermark"] == n

    rows = conn.execute(
        "SELECT seq, revision, text FROM finalized WHERE channel = ? ORDER BY seq",
        (channel,),
    ).fetchall()
    assert len(rows) == n  # 无重复定稿
    assert [r["seq"] for r in rows] == list(range(1, n + 1))  # 连续前缀，无缺口
    # 每个序号定稿的是定稿时刻已收到的最高修订号（rev2 若晚于定稿到达则被拒绝）
    assert all(r["revision"] in (1, 2) for r in rows)
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM fragments WHERE channel = ?", (channel,)
    ).fetchone()["c"] == 0  # 缓冲区已清空


# ---------- 永久缺口裁决 ----------


def test_gap_decision_records_skips_and_releases_following_buffer(client):
    # 1、2 已定稿；3、4 缺失；5、6 已缓冲
    submit(client, "c1", 1, 1, "one")
    submit(client, "c1", 2, 1, "two")
    submit(client, "c1", 5, 1, "five")
    submit(client, "c1", 6, 1, "six")

    body = decide_gap(client, "c1", 3, 4)
    assert body["applied"] is True
    assert body["idempotent"] is False
    assert body["reason"] is None
    assert body["watermark"] == 6
    assert body["skipped"] == {"start_seq": 3, "end_seq": 4}
    assert [i["seq"] for i in body["finalized"]] == [5, 6]
    assert body["finalized"][0]["kind"] == "subtitle"
    assert body["finalized"][0]["text"] == "five"


def test_gap_decision_without_following_buffer_stops_at_end(client):
    submit(client, "c1", 1, 1)
    body = decide_gap(client, "c1", 2, 3)
    assert body["applied"] is True
    assert body["watermark"] == 3
    assert body["finalized"] == []

    # 裁决后才到达的片段落在水位线内，不可再修改
    body = submit(client, "c1", 2, 1, "late")
    assert body["applied"] is False
    assert body["reason"] == "already_finalized"


def test_gap_start_must_equal_watermark_plus_one(client):
    submit(client, "c1", 1, 1)
    body = decide_gap(client, "c1", 3, 4)  # 水位线为 1，起点必须是 2
    assert body["applied"] is False
    assert body["idempotent"] is False
    assert body["reason"] == "start_not_at_watermark"
    assert body["watermark"] == 1
    assert body["skipped"] is None
    assert body["finalized"] == []


def test_gap_rejected_when_interval_has_buffered_fragments(client, conn):
    submit(client, "c1", 1, 1)
    submit(client, "c1", 3, 1, "buffered")  # 缺口区间内已有缓冲

    body = decide_gap(client, "c1", 2, 4)
    assert body["applied"] is False
    assert body["reason"] == "buffered_fragments"
    assert body["watermark"] == 1

    # 校验失败不得产生任何写入
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM gap_decisions WHERE channel = 'c1'"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM gaps WHERE channel = 'c1'"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT watermark FROM channels WHERE channel = 'c1'"
    ).fetchone()["watermark"] == 1
    # 缓冲片段原样保留，后续仍可正常定稿
    submit(client, "c1", 2, 1, "two")
    assert client.get("/channels/c1/finalized").json()["watermark"] == 3


def test_idempotent_repeat_of_same_decided_interval(client):
    submit(client, "c1", 1, 1, "one")
    submit(client, "c1", 4, 1, "four")
    submit(client, "c1", 5, 1, "five")

    first = decide_gap(client, "c1", 2, 3)
    assert first["watermark"] == 5
    assert [i["seq"] for i in first["finalized"]] == [4, 5]

    # 重复提交同一区间：幂等返回原裁决结果
    again = decide_gap(client, "c1", 2, 3)
    assert again["applied"] is True
    assert again["idempotent"] is True
    assert again["reason"] is None
    assert again["watermark"] == 5
    assert again["skipped"] == {"start_seq": 2, "end_seq": 3}
    assert [i["seq"] for i in again["finalized"]] == [4, 5]
    assert again["finalized"][0]["text"] == "four"


def test_idempotent_gap_without_released_fragments(client):
    decide_gap(client, "c1", 1, 2)
    again = decide_gap(client, "c1", 1, 2)
    assert again["applied"] is True
    assert again["idempotent"] is True
    assert again["watermark"] == 2
    assert again["finalized"] == []


def test_partial_overlap_with_decided_gap_is_rejected(client, conn):
    submit(client, "c1", 1, 1)
    decide_gap(client, "c1", 2, 4)  # 水位线推进到 4

    # 与既有裁决部分重叠（含）
    body = decide_gap(client, "c1", 3, 4)
    assert body["applied"] is False
    assert body["reason"] == "gap_overlap"
    assert body["watermark"] == 4
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM gap_decisions WHERE channel = 'c1'"
    ).fetchone()["c"] == 1  # 未新增裁决

    # 完全落在已定稿区域、且不匹配任何缺口
    body = decide_gap(client, "c1", 1, 1)
    assert body["applied"] is False
    assert body["reason"] == "already_finalized"


def test_repeated_rejected_gap_decision_writes_nothing(client, conn):
    submit(client, "c1", 1, 1)
    for _ in range(3):
        body = decide_gap(client, "c1", 3, 5)
        assert body["reason"] == "start_not_at_watermark"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM gap_decisions WHERE channel = 'c1'"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM gaps WHERE channel = 'c1'"
    ).fetchone()["c"] == 0


def test_finalized_query_interleaves_subtitles_and_gap_markers(client):
    submit(client, "c1", 1, 1, "one")
    submit(client, "c1", 5, 1, "five")
    decide_gap(client, "c1", 2, 4)

    resp = client.get("/channels/c1/finalized").json()
    assert resp["watermark"] == 5
    kinds = [(i["kind"], i["seq"]) for i in resp["items"]]
    assert kinds == [
        ("subtitle", 1),
        ("gap", 2),
        ("gap", 3),
        ("gap", 4),
        ("subtitle", 5),
    ]
    gap = resp["items"][1]
    assert gap["start_seq"] == 2
    assert gap["end_seq"] == 4
    assert gap["text"] is None
    assert gap["revision"] is None

    # after 增量拉取对字幕与缺口标记统一按序号计数
    resp = client.get("/channels/c1/finalized", params={"after": 3}).json()
    assert [(i["kind"], i["seq"]) for i in resp["items"]] == [
        ("gap", 4),
        ("subtitle", 5),
    ]


def test_multiple_gaps_and_submissions_interleave(client):
    submit(client, "c1", 1, 1, "one")
    decide_gap(client, "c1", 2, 2)
    submit(client, "c1", 3, 1, "three")
    submit(client, "c1", 6, 1, "six")
    decide_gap(client, "c1", 4, 5)

    resp = client.get("/channels/c1/finalized").json()
    assert resp["watermark"] == 6
    assert [(i["kind"], i["seq"]) for i in resp["items"]] == [
        ("subtitle", 1),
        ("gap", 2),
        ("subtitle", 3),
        ("gap", 4),
        ("gap", 5),
        ("subtitle", 6),
    ]

    # 两个相邻的独立缺口区间不可合并为一次幂等区间
    body = decide_gap(client, "c1", 2, 5)
    assert body["applied"] is False
    assert body["reason"] == "gap_overlap"
    # 各自仍可单独幂等返回
    assert decide_gap(client, "c1", 2, 2)["idempotent"] is True
    assert decide_gap(client, "c1", 4, 5)["idempotent"] is True


def test_gap_validation_rejects_malformed_payload(client):
    resp = client.post("/channels/c1/gaps", json={"start_seq": 0, "end_seq": 2})
    assert resp.status_code == 422
    resp = client.post("/channels/c1/gaps", json={"start_seq": 5, "end_seq": 3})
    assert resp.status_code == 422


def test_gap_decisions_are_isolated_per_channel(client):
    decide_gap(client, "a", 1, 2)
    submit(client, "b", 1, 1, "b-one")
    resp = client.get("/channels/a/finalized").json()
    assert resp["watermark"] == 2
    assert all(i["kind"] == "gap" for i in resp["items"])
    resp = client.get("/channels/b/finalized").json()
    assert resp["watermark"] == 1
    assert [i["kind"] for i in resp["items"]] == ["subtitle"]


def test_gap_on_unknown_channel_starts_at_one(client):
    body = decide_gap(client, "fresh", 1, 3)
    assert body["applied"] is True
    assert body["watermark"] == 3
    assert body["skipped"] == {"start_seq": 1, "end_seq": 3}

    body = decide_gap(client, "fresh", 5, 6)
    assert body["applied"] is False
    assert body["reason"] == "start_not_at_watermark"


# ---------- 定稿更正（不可变追加层） ----------


def correct(client, channel, seq, expected_version, text="fixed", reason="错别字"):
    resp = client.post(
        f"/channels/{channel}/corrections",
        json={
            "seq": seq,
            "expected_version": expected_version,
            "text": text,
            "reason": reason,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def test_correction_appends_consecutive_versions(client):
    submit(client, "c1", 1, 1, "orig")

    body = correct(client, "c1", 1, 0, "fix-1", "第一次更正")
    assert body["applied"] is True
    assert body["reason"] is None
    assert body["correction_version"] == 1
    assert body["current_version"] == 1

    body = correct(client, "c1", 1, 1, "fix-2", "第二次更正")
    assert body["applied"] is True
    assert body["correction_version"] == 2
    assert body["current_version"] == 2

    # 审计链按版本升序，文本与原因完整保留
    audit = client.get("/channels/c1/corrections/1").json()
    assert audit["finalized"] is True
    assert audit["correction_version"] == 2
    assert [c["version"] for c in audit["corrections"]] == [1, 2]
    assert [c["text"] for c in audit["corrections"]] == ["fix-1", "fix-2"]
    assert [c["reason"] for c in audit["corrections"]] == ["第一次更正", "第二次更正"]
    assert all(c["created_at"] for c in audit["corrections"])


def test_correction_rejected_for_unfinalized_seq(client, conn):
    submit(client, "c1", 2, 1, "buffered")  # 已缓冲但未定稿（缺口 1 未补齐）

    for seq in (1, 2, 99):  # 未到达 / 仍在缓冲 / 远超水位线
        body = correct(client, "c1", seq, 0, "nope")
        assert body["applied"] is False
        assert body["reason"] == "not_finalized"
        assert body["correction_version"] is None
        assert body["current_version"] == 0

    body = correct(client, "unknown", 1, 0, "nope")  # 频道不存在
    assert body["applied"] is False
    assert body["reason"] == "not_finalized"

    # 拒绝不得产生任何写入
    assert conn.execute("SELECT COUNT(*) AS c FROM corrections").fetchone()["c"] == 0


def test_correction_rejected_for_gap_seq(client, conn):
    submit(client, "c1", 1, 1, "one")
    decide_gap(client, "c1", 2, 3)

    body = correct(client, "c1", 2, 0, "nope")
    assert body["applied"] is False
    assert body["reason"] == "gap_seq"
    assert body["current_version"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM corrections").fetchone()["c"] == 0

    # 缺口前后的定稿字幕不受影响，仍可正常更正
    assert correct(client, "c1", 1, 0, "fixed")["applied"] is True


def test_correction_version_conflict_returns_current_version(client, conn):
    submit(client, "c1", 1, 1, "orig")

    # 当前版本为 0，期望 1 直接冲突
    body = correct(client, "c1", 1, 1, "skip")
    assert body["applied"] is False
    assert body["reason"] == "version_conflict"
    assert body["correction_version"] is None
    assert body["current_version"] == 0

    assert correct(client, "c1", 1, 0, "fix-1")["applied"] is True

    # 当前版本已为 1，再用 0 或 2 提交均冲突，并返回当前版本供重试
    for expected in (0, 2, 99):
        body = correct(client, "c1", 1, expected, "stale")
        assert body["applied"] is False
        assert body["reason"] == "version_conflict"
        assert body["current_version"] == 1

    # 冲突不产生写入：审计链仍只有一条
    audit = client.get("/channels/c1/corrections/1").json()
    assert [c["version"] for c in audit["corrections"]] == [1]
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM corrections WHERE channel = 'c1'"
    ).fetchone()["c"] == 1


def test_correction_never_rewrites_original(client, conn):
    submit(client, "c1", 1, 1, "原始文本")
    correct(client, "c1", 1, 0, "更正一")
    correct(client, "c1", 1, 1, "更正二")

    # 数据库中的定稿原文不变
    row = conn.execute(
        "SELECT revision, text FROM finalized WHERE channel = 'c1' AND seq = 1"
    ).fetchone()
    assert (row["revision"], row["text"]) == (1, "原始文本")

    # 默认视图（original）保持兼容，仍返回原文且不暴露更正版本
    resp = client.get("/channels/c1/finalized").json()
    item = resp["items"][0]
    assert item["text"] == "原始文本"
    assert item["correction_version"] is None

    resp = client.get("/channels/c1/finalized", params={"view": "original"}).json()
    assert resp["items"][0]["text"] == "原始文本"


def test_effective_view_merges_latest_correction(client):
    submit(client, "c1", 1, 1, "one")
    submit(client, "c1", 2, 1, "two")
    submit(client, "c1", 5, 1, "five")
    decide_gap(client, "c1", 3, 4)
    correct(client, "c1", 1, 0, "one-v2", "错字")
    correct(client, "c1", 1, 1, "one-v3", "再改")
    correct(client, "c1", 5, 0, "five-v2", "漏字")

    resp = client.get("/channels/c1/finalized", params={"view": "effective"}).json()
    assert resp["watermark"] == 5
    items = {i["seq"]: i for i in resp["items"]}
    # 合并最新更正文本与版本号
    assert items[1]["text"] == "one-v3"
    assert items[1]["correction_version"] == 2
    # 未更正的字幕保持原文，版本为 0
    assert items[2]["text"] == "two"
    assert items[2]["correction_version"] == 0
    # 缺口标记不受更正影响
    assert items[3]["kind"] == "gap"
    assert items[3]["correction_version"] is None
    assert items[5]["text"] == "five-v2"
    assert items[5]["correction_version"] == 1

    # effective 视图同样支持 after/limit 增量拉取
    resp = client.get(
        "/channels/c1/finalized", params={"view": "effective", "after": 1, "limit": 2}
    ).json()
    assert [i["seq"] for i in resp["items"]] == [2, 3]

    # 非法视图返回 422
    assert client.get("/channels/c1/finalized", params={"view": "bogus"}).status_code == 422


def test_correction_audit_for_seq_without_corrections(client):
    submit(client, "c1", 1, 1, "orig")

    audit = client.get("/channels/c1/corrections/1").json()
    assert audit["finalized"] is True
    assert audit["correction_version"] == 0
    assert audit["corrections"] == []

    # 未定稿序号：空链且 finalized=false
    audit = client.get("/channels/c1/corrections/7").json()
    assert audit["finalized"] is False
    assert audit["correction_version"] == 0
    assert audit["corrections"] == []


def test_correction_validation_rejects_bad_input(client):
    submit(client, "c1", 1, 1, "orig")
    base = "/channels/c1/corrections"
    resp = client.post(base, json={"seq": 0, "expected_version": 0, "text": "x", "reason": "r"})
    assert resp.status_code == 422
    resp = client.post(base, json={"seq": 1, "expected_version": -1, "text": "x", "reason": "r"})
    assert resp.status_code == 422
    resp = client.post(base, json={"seq": 1, "expected_version": 0, "text": "x", "reason": ""})
    assert resp.status_code == 422
    resp = client.post(base, json={"seq": 1, "expected_version": 0, "text": "x"})
    assert resp.status_code == 422


def test_concurrent_corrections_single_winner_per_version(client, conn):
    """多线程以相同 expected_version 竞争：每个版本只有一个追加成功。"""
    submit(client, "race", 1, 1, "orig")

    def race(expected):
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(
                pool.map(
                    lambda i: service.submit_correction(
                        "race", 1, expected, f"fix-{expected}-{i}", "并发更正"
                    ),
                    range(16),
                )
            )

    for version in (1, 2, 3):
        results = race(version - 1)
        winners = [r for r in results if r["applied"]]
        losers = [r for r in results if not r["applied"]]
        assert len(winners) == 1
        assert winners[0]["correction_version"] == version
        assert len(losers) == 15
        assert all(r["reason"] == "version_conflict" for r in losers)
        assert all(r["current_version"] == version for r in losers)

    rows = conn.execute(
        "SELECT version FROM corrections WHERE channel = 'race' AND seq = 1 ORDER BY version"
    ).fetchall()
    assert [r["version"] for r in rows] == [1, 2, 3]  # 链连续无重复
    # 并发更正全程不改写原定稿
    assert conn.execute(
        "SELECT text FROM finalized WHERE channel = 'race' AND seq = 1"
    ).fetchone()["text"] == "orig"
