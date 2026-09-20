from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class FragmentIn(BaseModel):
    seq: int = Field(..., ge=1, description="片段序号，从 1 开始连续编号")
    revision: int = Field(..., ge=0, description="修订号，同一序号只接受更大的值")
    text: str = Field(..., description="字幕文本")


class GapDecisionIn(BaseModel):
    start_seq: int = Field(..., ge=1, description="缺口起点序号，必须等于当前水位线下一位")
    end_seq: int = Field(..., ge=1, description="缺口终点序号（含），不得小于起点")

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "GapDecisionIn":
        if self.end_seq < self.start_seq:
            raise ValueError("end_seq 不得小于 start_seq")
        return self


class FinalizedItem(BaseModel):
    kind: str = Field("subtitle", description="条目类型：subtitle=字幕；gap=永久缺口标记")
    seq: int
    revision: Optional[int] = Field(None, description="修订号（仅字幕条目存在）")
    text: Optional[str] = Field(None, description="字幕文本（仅字幕条目存在）")
    start_seq: Optional[int] = Field(None, description="缺口区间起点（仅缺口条目存在）")
    end_seq: Optional[int] = Field(None, description="缺口区间终点（仅缺口条目存在）")


class SubmitResponse(BaseModel):
    channel: str
    seq: int
    applied: bool = Field(..., description="本次提交是否生效（被接受并写入缓冲区）")
    reason: Optional[str] = Field(
        None,
        description="未生效原因：stale_revision=修订号不够新；already_finalized=序号已定稿",
    )
    watermark: int = Field(..., description="提交处理完成后的最新水位线")
    finalized: List[FinalizedItem] = Field(
        default_factory=list, description="本次提交触发新定稿的片段（缺口补齐时可能一次释放多条）"
    )


class SkippedGap(BaseModel):
    start_seq: int
    end_seq: int


class GapDecisionResponse(BaseModel):
    channel: str
    applied: bool = Field(..., description="裁决是否生效；false 时不产生任何写入")
    idempotent: bool = Field(..., description="是否为重复提交已完整裁决区间的幂等返回")
    reason: Optional[str] = Field(
        None,
        description=(
            "未生效原因：start_not_at_watermark=起点不是水位线下一位；"
            "buffered_fragments=区间内存在已缓冲片段；"
            "already_finalized=区间已完全定稿；gap_overlap=与既有裁决部分重叠"
        ),
    )
    watermark: int = Field(..., description="裁决处理完成后的最新水位线")
    skipped: Optional[SkippedGap] = Field(None, description="本次（或原裁决）标记永久丢失的区间")
    finalized: List[FinalizedItem] = Field(
        default_factory=list, description="本次（或原裁决时）随裁决新定稿的连续片段"
    )


class FinalizedResponse(BaseModel):
    channel: str
    watermark: int
    items: List[FinalizedItem] = Field(
        ..., description="按序号升序混合返回字幕（kind=subtitle）与缺口标记（kind=gap）"
    )
