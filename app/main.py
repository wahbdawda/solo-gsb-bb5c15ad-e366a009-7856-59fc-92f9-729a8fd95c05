from contextlib import asynccontextmanager

from fastapi import FastAPI, Query

from . import service
from .db import init_db
from .schemas import (
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
    summary="直播字幕定稿服务：乱序片段缓冲、按连续前缀推进水位线并定稿",
    version="1.0.0",
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
) -> dict:
    """查询频道已定稿字幕、永久缺口标记与当前水位线（按序号升序混合返回）。"""
    return service.get_finalized(channel, after=after, limit=limit)
