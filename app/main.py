from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Query

from . import service
from .db import init_db
from .schemas import (
    CorrectionHistoryResponse,
    CorrectionIn,
    CorrectionResponse,
    FinalizedResponse,
    FragmentIn,
    GapDecisionIn,
    GapDecisionResponse,
    SubmitResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时自动初始化数据库（建表幂等）
    init_db()
    yield


app = FastAPI(
    title="Live Subtitle Finalizer",
    summary="直播字幕定稿服务：乱序片段缓冲、按连续前缀推进水位线并定稿，定稿后支持追加式更正",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/channels/{channel}/fragments", response_model=SubmitResponse)
def submit_fragment(channel: str, body: FragmentIn) -> dict:
    """提交字幕片段。片段可乱序到达；同一序号只接受更高修订号。"""
    return service.submit_fragment(channel, body.seq, body.revision, body.text)


@app.post("/channels/{channel}/gaps", response_model=GapDecisionResponse)
def decide_gap(channel: str, body: GapDecisionIn) -> dict:
    """裁决永久缺口。

    区间起点必须等于当前水位线下一位，且区间内不得存在已缓冲片段；
    成功后跳过该区间并继续释放紧随其后的连续缓冲片段。重复提交
    已完整裁决的同一区间幂等返回，部分重叠拒绝。
    """
    return service.decide_gap(channel, body.start_seq, body.end_seq)


@app.get("/channels/{channel}/finalized", response_model=FinalizedResponse)
def get_finalized(
    channel: str,
    after: int = Query(0, ge=0, description="只返回序号大于该值的定稿片段与缺口标记"),
    limit: int = Query(1000, ge=1, le=10000),
    view: Literal["original", "effective"] = Query(
        "original",
        description="original=定稿原文（默认）；effective=合并最新更正文本并返回 correction_version",
    ),
) -> dict:
    """查询频道已定稿字幕、永久缺口标记与当前水位线（按序号升序混合返回）。"""
    return service.get_finalized(channel, after=after, limit=limit, view=view)


@app.post("/channels/{channel}/corrections", response_model=CorrectionResponse)
def submit_correction(channel: str, body: CorrectionIn) -> dict:
    """对已定稿字幕追加一条不可变更正。

    仅允许更正已定稿字幕（缺口或未定稿序号拒绝且不写入）；
    expected_version 必须等于当前更正版本（无更正时为 0），成功时原子
    追加下一版本，竞争请求返回版本冲突；原定稿记录永不改写。
    """
    return service.submit_correction(
        channel, body.seq, body.expected_version, body.text, body.reason
    )


@app.get(
    "/channels/{channel}/corrections/{seq}",
    response_model=CorrectionHistoryResponse,
)
def get_corrections(channel: str, seq: int) -> dict:
    """查询指定序号的更正审计链，按版本升序返回全部更正记录。"""
    return service.get_corrections(channel, seq)
