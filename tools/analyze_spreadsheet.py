"""Excel/CSV 读取、概况、筛选与分组统计；完整数据不进入模型上下文。"""

import asyncio
import json
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID
from zipfile import ZipFile

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from core.exceptions import FileTooLargeError, UnsupportedFileError
from storage import documents
from storage.file_data import get_file_data

SPREADSHEET_TYPES = {"xlsx", "xls", "csv"}
MAX_ROWS = 100_000
MAX_COLUMNS = 100
MAX_SHEETS = 20
MAX_TOTAL_CELLS = 2_000_000


class SpreadsheetArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: UUID = Field(description="当前会话中的 Excel/CSV 文件 ID")
    sheet: str | Annotated[int, Field(strict=True, ge=0)] = Field(
        default=0, description="工作表名称或从 0 开始的索引；CSV 忽略此参数",
    )
    operation: Literal["sheets", "preview", "profile", "aggregate"] = "profile"
    column: str | None = Field(default=None, max_length=200)
    metric: Literal["sum", "mean", "min", "max", "count"] = "sum"
    group_by: str | None = Field(default=None, max_length=200)
    filters: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict, max_length=10,
        description="按列等值筛选，多个条件为 AND；null 表示空值",
    )
    sort: Literal["asc", "desc"] | None = Field(
        default=None, description="聚合结果按统计值排序，再取 limit 条",
    )
    limit: int = Field(default=10, strict=True, ge=1, le=20)


SPREADSHEET_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_spreadsheet",
        "description": (
            "分析当前会话 Excel/CSV 的完整匹配数据。sheets 列出工作表；profile 查看列名、"
            "缺失值和数值概况；preview 查看样例；aggregate 执行等值筛选、分组和数值统计。"
            "count 省略 column 统计行数，指定 column 统计非空值。limit 只限制展示，"
            "聚合处理全部匹配行。sort=desc 可查最大值分组。表格统计必须调用此工具，"
            "不能用文档预览片段估算。常规浮点统计不保证财务小数精确性。"
        ),
        "parameters": SpreadsheetArguments.model_json_schema(),
    },
}


@lru_cache(maxsize=1)
def _read_workbook(filename: str, raw: bytes) -> dict[str, pd.DataFrame]:
    """只缓存最近一个工作簿，重启后仍可从原文件重新加载。"""
    if not raw:
        raise ValueError("表格文件不能为空。")
    if len(raw) > 20 * 1024 * 1024:
        raise FileTooLargeError()

    total_cells = 0

    def check(frame):
        nonlocal total_cells
        if len(frame) > MAX_ROWS or len(frame.columns) > MAX_COLUMNS:
            raise FileTooLargeError()
        total_cells += len(frame) * len(frame.columns)
        if total_cells > MAX_TOTAL_CELLS:
            raise FileTooLargeError()
        frame.columns = frame.columns.map(str)
        if frame.columns.duplicated().any() or any(len(name) > 200 for name in frame.columns):
            raise ValueError("表头不能重复，且每个列名最多 200 字。")
        return frame

    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        # 先完整解码，避免 nrows 让文件后半段的编码错误被漏掉。
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("CSV 编码不是 UTF-8 或 GB18030。")
        if "\x00" in text:
            raise ValueError("CSV 包含非法空字符。")
        frames = {"CSV": check(pd.read_csv(BytesIO(text.encode("utf-8")), nrows=MAX_ROWS + 1))}
    elif suffix in {".xlsx", ".xls"}:
        if suffix == ".xlsx":
            with ZipFile(BytesIO(raw)) as archive:
                members = archive.infolist()
                if len(members) > 2000 or sum(item.file_size for item in members) > 100 * 1024 * 1024:
                    raise FileTooLargeError()
        with pd.ExcelFile(
            BytesIO(raw), engine="openpyxl" if suffix == ".xlsx" else "xlrd",
        ) as workbook:
            if not 1 <= len(workbook.sheet_names) <= MAX_SHEETS:
                raise FileTooLargeError()
            frames = {}
            for name in workbook.sheet_names:
                # 每张表读完立即检查，避免先加载全部超大工作表。
                frames[name] = check(pd.read_excel(workbook, sheet_name=name, nrows=MAX_ROWS + 1))
    else:
        raise UnsupportedFileError()

    return frames


def _sheet_name(frames: dict, sheet: str | int, csv: bool = False) -> str:
    if isinstance(sheet, bool) or not isinstance(sheet, (str, int)):
        raise ValueError("sheet 必须是名称或从 0 开始的索引。")
    names = list(frames)
    if csv:
        return "CSV"
    if isinstance(sheet, int):
        if not 0 <= sheet < len(names):
            raise ValueError("工作表索引超出范围，请先使用 sheets 查看工作表。")
        return names[sheet]
    if sheet not in frames:
        raise ValueError("工作表名称不存在，请先使用 sheets 查看工作表。")
    return sheet


def _read_sheet(filename: str, raw: bytes, sheet: str | int) -> pd.DataFrame:
    frames = _read_workbook(filename, raw)
    return frames[_sheet_name(frames, sheet, Path(filename).suffix.lower() == ".csv")]


def _records(frame: pd.DataFrame) -> list[dict]:
    # 把 NumPy 类型、时间和 NaN 转成 JSON 可用值，缺失值使用 null。
    return json.loads(frame.to_json(
        orient="records", date_format="iso", force_ascii=False, double_precision=15,
    ))


def spreadsheet_sections(filename: str, raw: bytes) -> list[tuple[str, str]]:
    """上传时校验全部工作表，只生成表头和前 5 行预览用于附件展示。"""
    frames = _read_workbook(filename, raw)
    return [
        (
            f"工作表 {name}"[:100],
            "表格预览（不代表完整数据）\n" + json.dumps({
                "sheet": name, "rows": len(frame), "columns": list(frame.columns),
                "preview": _records(frame.head(5)),
            }, ensure_ascii=False)[:6000],
        )
        for name, frame in frames.items()
    ]


def _analyze_spreadsheet(
    filename: str, raw: bytes, sheet: str | int, operation: str,
    column: str | None, metric: str, group_by: str | None,
    filters: dict | None, limit: int, sort: str | None = None,
) -> dict:
    if operation not in {"sheets", "preview", "profile", "aggregate"}:
        raise ValueError("operation 必须为 sheets、preview、profile 或 aggregate。")
    if metric not in {"sum", "mean", "min", "max", "count"}:
        raise ValueError("metric 必须为 sum、mean、min、max 或 count。")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit 必须是 1～20 的整数。")
    if sort not in {None, "asc", "desc"}:
        raise ValueError("sort 必须为 asc、desc 或 null。")
    if filters is not None and (
        not isinstance(filters, dict) or len(filters) > 10
        or any(not isinstance(key, str) or (value is not None and type(value) not in (str, int, float, bool))
               for key, value in filters.items())
    ):
        raise ValueError("filters 必须是最多 10 个按列匹配的简单值。")

    frames = _read_workbook(filename, raw)
    sheets = [
        {"name": name, "rows": len(frame), "columns": len(frame.columns)}
        for name, frame in frames.items()
    ]
    if operation == "sheets":
        return {"filename": filename, "operation": operation, "sheets": sheets}

    name = _sheet_name(frames, sheet, Path(filename).suffix.lower() == ".csv")
    frame = frames[name]
    total_rows = len(frame)
    for key, value in (filters or {}).items():
        if key not in frame.columns:
            raise ValueError(f"筛选列不存在：{key[:200]}")
        mask = frame[key].isna() if value is None else frame[key].eq(value)
        frame = frame.loc[mask]

    result = {
        "filename": filename, "sheet": name, "sheets": sheets,
        "total_rows": total_rows, "matched_rows": len(frame),
        "columns": list(frame.columns), "operation": operation, "filters": filters or {},
    }
    if operation == "preview":
        return {**result, "rows": _records(frame.head(limit)), "truncated": len(frame) > limit}
    if operation == "profile":
        numeric = frame.select_dtypes(include="number")
        stats = (
            # 每行对应一个数值列，避免用户列名与 statistic 等元数据字段冲突。
            numeric.describe().T.rename_axis("column").reset_index()
            if not numeric.empty else pd.DataFrame()
        )
        return {
            **result,
            "dtypes": {key: str(dtype) for key, dtype in frame.dtypes.items()},
            "missing": {key: int(count) for key, count in frame.isna().sum().items()},
            "numeric_summary": _records(stats),
            "preview": _records(frame.head(limit)), "preview_truncated": len(frame) > limit,
        }

    if column is not None and column not in frame.columns:
        raise ValueError(f"统计列不存在：{column[:200]}")
    if metric != "count" and column is None:
        raise ValueError("sum、mean、min、max 必须指定 column。")
    if group_by is not None and group_by not in frame.columns:
        raise ValueError(f"分组列不存在：{group_by[:200]}")
    values = frame[column] if column is not None else pd.Series(1, index=frame.index)
    if metric != "count":
        values = pd.to_numeric(values, errors="raise")
        if values.isin([float("inf"), float("-inf")]).any():
            raise ValueError("统计列包含无穷大。")
        if pd.api.types.is_integer_dtype(values.dtype):
            # int64 求和可能溢出；转成 Python 整数对象后再聚合。
            values = values.astype(object)
    if group_by is not None:
        work = pd.DataFrame({"group": frame[group_by], "value": values})
        grouped = work.groupby("group", dropna=False, sort=False)["value"]
        aggregated = grouped.sum(min_count=1) if metric == "sum" else getattr(grouped, metric)()
        table = aggregated.reset_index(name="value")
    else:
        value = values.sum(min_count=1) if metric == "sum" else getattr(values, metric)()
        table = pd.DataFrame({"value": [value]})
    if table["value"].isin([float("inf"), float("-inf")]).any():
        raise ValueError("聚合结果超出数值范围。")
    if sort is not None:
        table = table.sort_values("value", ascending=sort == "asc", kind="stable", na_position="last")
    return {
        **result, "column": column, "metric": metric, "group_by": group_by, "sort": sort,
        "group_count": len(table) if group_by is not None else None,
        "rows": _records(table.head(limit)), "truncated": len(table) > limit,
        "numeric_precision": "常规数值统计；浮点数不保证财务小数精确性",
    }


async def analyze_spreadsheet(
    filename: str, raw: bytes, sheet: str | int = 0, operation: str = "profile",
    column: str | None = None, metric: str = "sum", group_by: str | None = None,
    filters: dict | None = None, limit: int = 10, sort: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        _analyze_spreadsheet, filename, raw, sheet, operation, column, metric,
        group_by, filters, limit, sort,
    )


async def analyze_spreadsheet_file(file_id: str, **kwargs) -> dict:
    """Agent 先校验会话范围，再通过文件 ID 读取服务器保存的原始表格。"""
    metadata = await asyncio.to_thread(documents.get_document, file_id)
    if metadata["file_type"] not in SPREADSHEET_TYPES:
        raise ValueError("该文件不是 Excel/CSV 表格。")
    raw = await asyncio.to_thread(get_file_data, file_id)
    result = await analyze_spreadsheet(metadata["filename"], raw, **kwargs)
    return {"source_type": "spreadsheet", "file_id": file_id, **result}
