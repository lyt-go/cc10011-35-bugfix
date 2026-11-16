"""接口出入参模型：列表分页、动作结果与各模块的明细结构。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20
    extra: dict[str, Any] = Field(default_factory=dict)


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    """登记或修改一条业务记录时提交的字段集合。"""

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None



class RealtimemetricEntry(BaseModel):
    """指标定义明细结构。"""

    field_0: str | None = None  # 指标编号
    field_1: str | None = None  # 指标名称
    field_2: str | None = None  # 计算窗口秒
    field_3: str | None = None  # 聚合函数
    field_4: str | None = None  # 数据源流
    field_5: str | None = None  # 维度字段组
    field_6: str | None = None  # 延迟容忍毫秒
    field_7: str | None = None  # 精度位数

class DashboardEntry(BaseModel):
    """大盘配置明细结构。"""

    field_0: str | None = None  # 大盘编号
    field_1: str | None = None  # 大盘名称
    field_2: str | None = None  # 包含指标组
    field_3: str | None = None  # 布局模板
    field_4: str | None = None  # 刷新频率秒
    field_5: str | None = None  # 对比周期
    field_6: str | None = None  # 分享范围
    field_7: str | None = None  # 负责人

class DrilldownEntry(BaseModel):
    """下钻路径明细结构。"""

    field_0: str | None = None  # 路径编号
    field_1: str | None = None  # 起始维度
    field_2: str | None = None  # 下钻层级序列
    field_3: str | None = None  # 每层聚合粒度
    field_4: str | None = None  # 默认度量
    field_5: str | None = None  # 路径深度
    field_6: str | None = None  # 缓存秒数
    field_7: str | None = None  # 数据新鲜度

class ThresholdEntry(BaseModel):
    """阈值规则明细结构。"""

    field_0: str | None = None  # 规则编号
    field_1: str | None = None  # 关联指标
    field_2: str | None = None  # 阈值下限
    field_3: str | None = None  # 阈值上限
    field_4: str | None = None  # 触发条件
    field_5: str | None = None  # 持续周期
    field_6: str | None = None  # 告警等级
    field_7: str | None = None  # 生效时间范围

class AnomalydetectEntry(BaseModel):
    """检测模型明细结构。"""

    field_0: str | None = None  # 模型编号
    field_1: str | None = None  # 监控指标
    field_2: str | None = None  # 检测算法
    field_3: str | None = None  # 训练窗口天
    field_4: str | None = None  # 灵敏度参数
    field_5: str | None = None  # 季节周期
    field_6: str | None = None  # 节假日因子
    field_7: str | None = None  # 模型更新时间

class DataserviceEntry(BaseModel):
    """服务接口明细结构。"""

    field_0: str | None = None  # 服务编号
    field_1: str | None = None  # 服务名称
    field_2: str | None = None  # 接口路径
    field_3: str | None = None  # 查询参数
    field_4: str | None = None  # 返回字段
    field_5: str | None = None  # 调用频次限制
    field_6: str | None = None  # 缓存策略
    field_7: str | None = None  # 服务版本

class AlarmhistoryEntry(BaseModel):
    """告警记录明细结构。"""

    field_0: str | None = None  # 记录编号
    field_1: str | None = None  # 告警规则
    field_2: str | None = None  # 触发时间
    field_3: str | None = None  # 当前值
    field_4: str | None = None  # 阈值对比
    field_5: str | None = None  # 恢复时间
    field_6: str | None = None  # 持续时长
    field_7: str | None = None  # 处置状态

class ComparisonEntry(BaseModel):
    """对比配置明细结构。"""

    field_0: str | None = None  # 配置编号
    field_1: str | None = None  # 目标指标
    field_2: str | None = None  # 对比类型
    field_3: str | None = None  # 基准周期
    field_4: str | None = None  # 偏移量
    field_5: str | None = None  # 差异阈值
    field_6: str | None = None  # 显示格式
    field_7: str | None = None  # 对比维度

class AggregateEntry(BaseModel):
    """聚合规则明细结构。"""

    field_0: str | None = None  # 规则编号
    field_1: str | None = None  # 源数据粒度
    field_2: str | None = None  # 目标粒度
    field_3: str | None = None  # 聚合函数集
    field_4: str | None = None  # 数据偏移分钟
    field_5: str | None = None  # 补全策略
    field_6: str | None = None  # 空值处理
    field_7: str | None = None  # 输出数据集

class FilterpresetEntry(BaseModel):
    """预设模板明细结构。"""

    field_0: str | None = None  # 预设编号
    field_1: str | None = None  # 预设名称
    field_2: str | None = None  # 维度条件组
    field_3: str | None = None  # 时间范围
    field_4: str | None = None  # 排序规则
    field_5: str | None = None  # 显示列
    field_6: str | None = None  # 共享状态
    field_7: str | None = None  # 使用次数

class UserinsightEntry(BaseModel):
    """洞察报告明细结构。"""

    field_0: str | None = None  # 报告编号
    field_1: str | None = None  # 分析主题
    field_2: str | None = None  # 目标人群
    field_3: str | None = None  # 关键指标
    field_4: str | None = None  # 核心发现
    field_5: str | None = None  # 数据区间
    field_6: str | None = None  # 生成时间
    field_7: str | None = None  # 下次生成

class EventanalysisEntry(BaseModel):
    """分析模型明细结构。"""

    field_0: str | None = None  # 模型编号
    field_1: str | None = None  # 事件名称
    field_2: str | None = None  # 分组维度
    field_3: str | None = None  # 度量指标
    field_4: str | None = None  # 时间粒度
    field_5: str | None = None  # 归因窗口
    field_6: str | None = None  # 置信水平
    field_7: str | None = None  # 最小样本量

class SegmentprofileEntry(BaseModel):
    """画像定义明细结构。"""

    field_0: str | None = None  # 画像编号
    field_1: str | None = None  # 分群名称
    field_2: str | None = None  # 筛选规则
    field_3: str | None = None  # 画像维度
    field_4: str | None = None  # 对比基准
    field_5: str | None = None  # 更新周期
    field_6: str | None = None  # 覆盖对象数
    field_7: str | None = None  # 画像快照

class TrendforecastEntry(BaseModel):
    """预测方案明细结构。"""

    field_0: str | None = None  # 方案编号
    field_1: str | None = None  # 预测指标
    field_2: str | None = None  # 算法模型
    field_3: str | None = None  # 历史窗口天
    field_4: str | None = None  # 预测窗口天
    field_5: str | None = None  # 置信区间
    field_6: str | None = None  # 季节性开关
    field_7: str | None = None  # 外部因子

class ViztemplateEntry(BaseModel):
    """图表模板明细结构。"""

    field_0: str | None = None  # 模板编号
    field_1: str | None = None  # 图表类型
    field_2: str | None = None  # 数据绑定
    field_3: str | None = None  # 颜色方案
    field_4: str | None = None  # 坐标轴配置
    field_5: str | None = None  # 图例位置
    field_6: str | None = None  # 交互行为
    field_7: str | None = None  # 导出格式

class SubscriptionEntry(BaseModel):
    """订阅配置明细结构。"""

    field_0: str | None = None  # 订阅编号
    field_1: str | None = None  # 订阅内容
    field_2: str | None = None  # 接收人
    field_3: str | None = None  # 推送渠道
    field_4: str | None = None  # 推送频率
    field_5: str | None = None  # 数据粒度
    field_6: str | None = None  # 静默时段
    field_7: str | None = None  # 下次推送时间

class DimensionmapEntry(BaseModel):
    """映射规则明细结构。"""

    field_0: str | None = None  # 规则编号
    field_1: str | None = None  # 原始维度名
    field_2: str | None = None  # 标准维度名
    field_3: str | None = None  # 映射类型
    field_4: str | None = None  # 值转换逻辑
    field_5: str | None = None  # 默认取值
    field_6: str | None = None  # 映射优先级
    field_7: str | None = None  # 最近更新时间

class QualityscoreEntry(BaseModel):
    """评分规则明细结构。"""

    field_0: str | None = None  # 规则编号
    field_1: str | None = None  # 数据集名称
    field_2: str | None = None  # 完整性权重
    field_3: str | None = None  # 准确性权重
    field_4: str | None = None  # 时效性权重
    field_5: str | None = None  # 一致性权重
    field_6: str | None = None  # 评分周期
    field_7: str | None = None  # 总分数
