"""本轮选池专用的只读查询：先板块简表，再按代码展开同一份快照。"""

from typing import Any

from minisweagent.environments.miniqmt import MiniQMTClient

BRIEF_FIELDS = (
    "stock_code",
    "name",
    "last_price",
    "change_pct",
    "buyable",
    "unbuyable",
    "trend_gate",
    "vol_ratio",
    "high_20d_gap_pct",
)


def validate_candidate_query(args: dict) -> str:
    """解析层与宿主共用参数限制，避免模型绕过批量上限。"""
    keys = set(args)
    if keys not in ({"sectors"}, {"stock_codes"}):
        return "只能提供 sectors 或 stock_codes 其中一个数组。"
    key = next(iter(keys))
    values = args[key]
    maximum = 4 if key == "sectors" else 10
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        return f"{key} 必须是 1 到 {maximum} 项的数组。"
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return f"{key} 每项必须是非空字符串。"
    if len(set(values)) != len(values):
        return f"{key} 不能重复。"
    return ""


class CandidateDetails:
    def __init__(
        self, client: MiniQMTClient, catalog: dict[str, dict], errors: list[str], *,
        max_sectors: int, rows_per_sector: int, max_calls: int, max_stocks: int,
    ):
        self.client = client
        self.catalog = catalog
        self.errors = errors
        self.max_sectors = max_sectors
        self.rows_per_sector = rows_per_sector
        self.max_calls = max_calls
        self.max_stocks = max_stocks
        self.calls = 0
        self.sectors: dict[str, dict] = {}
        self.rows: dict[str, dict] = {}
        self.detailed: set[str] = set()
        self.pool: dict[str, dict[str, dict]] = {}

    def execute(self, args: dict) -> dict[str, Any]:
        self.calls += 1
        if self.calls > self.max_calls:
            return self._error("query_limit", "本轮查询次数已用完，请仅从已展开详情的股票中提交清单。")
        error = validate_candidate_query(args)
        if error:
            return self._error("invalid_argument", error)
        if "sectors" in args:
            names = args["sectors"]
            unknown = [name for name in names if name not in self.catalog]
            if unknown:
                return self._error("out_of_scope", f"板块不在本轮榜单中：{'、'.join(unknown)}")
            fresh = [name for name in names if name not in self.sectors]
            if len(self.sectors) + len(fresh) > self.max_sectors:
                return self._error("query_limit", f"本轮最多查询 {self.max_sectors} 个不同板块。")
            for name in fresh:
                self._sector(name)
            return self._success(
                sectors=[self.sectors[name] for name in fresh],
                already_returned=[name for name in names if name not in fresh],
            )
        codes = args["stock_codes"]
        unknown = [code for code in codes if code not in self.rows]
        if unknown:
            return self._error("out_of_scope", f"股票必须来自已查询板块的简表：{'、'.join(unknown)}")
        fresh = [code for code in codes if code not in self.detailed]
        if len(self.detailed) + len(fresh) > self.max_stocks:
            return self._error("query_limit", f"本轮最多展开 {self.max_stocks} 只不同股票的详情。")
        self.detailed.update(fresh)
        self._update_pool()
        return self._success(
            stocks=[self.rows[code] for code in fresh],
            already_returned=[code for code in codes if code not in fresh],
        )

    def _sector(self, name: str) -> None:
        sector = self.catalog[name]
        result = {"sector": name, "family": sector["family"], "rows": [], "errors": []}
        # 失败同样占预算并缓存，避免连续重试让盘前无限拖延。
        self.sectors[name] = result
        codes = sector.get("member_codes") or []
        try:
            if not codes:
                raise ValueError("无可用成分股代码")
            response = self.client.screen(
                stock_codes=codes, sort_by="close_position_desc", limit=self.rows_per_sector, enrich_trend=True,
            )
            if not response["ok"]:
                raise ValueError((response.get("error") or {}).get("detail", "个股查询失败"))
            data = response["data"]
            result["quote_at"] = data["quote_at"]
            result["errors"].extend(data.get("trend_errors") or [])
            for row in data["rows"]:
                code = row["stock_code"]
                if code not in codes:
                    result["errors"].append(f"{code} 不属于本板块成分，已丢弃")
                    continue
                # 跨板块重票始终复用第一次取得的完整行情，带上原始时间戳，不能混成新旧字段拼接。
                snapshot = self.rows.setdefault(code, {**row, "quote_at": data["quote_at"]})
                result["rows"].append({
                    **{key: snapshot[key] for key in BRIEF_FIELDS if key in snapshot},
                    "quote_at": snapshot["quote_at"],
                })
            if not result["rows"]:
                result["errors"].append("没有可用个股行情")
        except Exception as exc:
            result["errors"].append(f"{type(exc).__name__}: {exc}")
        self.errors.extend(f"板块 {name}：{error}" for error in result["errors"])
        self._update_pool()

    def _update_pool(self) -> None:
        # 最终校验只看到已向模型返回过完整详情的股票，不能跳过细查直接凭简表提交。
        self.pool = {
            name: {
                row["stock_code"]: self.rows[row["stock_code"]]
                for row in sector["rows"] if row["stock_code"] in self.detailed
            }
            for name, sector in self.sectors.items()
        }

    def _success(self, **data: Any) -> dict:
        return {
            "ok": True,
            "data": {
                **data,
                "remaining_calls": max(0, self.max_calls - self.calls),
                "remaining_sectors": self.max_sectors - len(self.sectors),
                "remaining_stocks": self.max_stocks - len(self.detailed),
            },
        }

    @staticmethod
    def _error(code: str, detail: str) -> dict:
        return {"ok": False, "error": {"code": code, "detail": detail}}
