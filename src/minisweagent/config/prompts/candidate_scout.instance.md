{{ task }}

## 大盘指数（{{ as_of }}）

{{ indexes }}

## 板块热度摘要（概念与行业）

{{ sector_ranks }}

## 账户摘要

{{ account }}

## 宿主硬限额

{{ limits }}

## 本轮查询上限

{{ query_limits }}

## 本次取数的失败与缺口

{{ data_errors }}

先通过 candidate_details 查询看好的板块简表，再展开拟入选股票的完整详情。最多选出 {{ sector_count }} 个板块、每个板块最多 {{ picks_per_sector }} 只票，可以少选，不要为了填满名额放宽标准。查询完成后最终只输出 JSON 对象。
