# deliverables

对外静态交付（HTML、PNG、回执、JSON），目录内容不入库（仅本 README 跟踪）。

固定展示页面的源文件在 `src/inv_trend/observability/静态展示/pages/`；本目录只保留
运行时生成产物与 `static-pages export` 的导出副本。

## 谁写入

交付流程写入最终静态文件；`scripts/美股前100指标.py` 写入美股前100看板、数据 JSON
和回执；`static-pages export` 写入固定页面副本。

## 谁读取

交付对象由使用者直接打开；程序不从本目录读取实现或权威数据。

## 什么可以删

已交付且无引用的静态文件可按交付记录清理；源码、权威数据、配置不得放本目录。
