{{ task }}

## 大盘指数（{{ as_of }}）

{{ indexes }}

## 板块热度榜

{{ sector_ranks }}

## 热门板块内部个股行情与趋势字段

{{ sector_candidates }}

## 账户

{{ account }}

## 宿主硬限额

{{ limits }}

## 前一交易日账本

{{ journal_previous }}

## 本次取数的失败与缺口

{{ data_errors }}

按系统提示的标准最多选出 {{ sector_count }} 个板块、每个板块最多 {{ picks_per_sector }} 只票。这是数量上限，可以少选，不要为了填满名额放宽标准。只输出 JSON 对象。
