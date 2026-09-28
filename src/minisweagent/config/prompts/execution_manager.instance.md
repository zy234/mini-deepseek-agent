{{ task }}

## 本轮读图结论（{{ as_of }}）

{{ readings }}

## 大盘指数

{{ indexes }}

## 账户快照、当日委托与成交

{{ account }}

## 宿主硬限额

{{ limits }}

## 今日交易账本

账本视图已压缩：最近一轮保留完整记录，更早轮次只保留实际有交易操作的记录；原始账本仍由宿主完整保存。

{{ journal_today }}

## 本轮取数与读图的失败与缺口

{{ data_errors }}

按系统提示裁决并执行，最后必须用 account_journal 追加本轮记录，周期 id 是 {{ cycle_id }}。
