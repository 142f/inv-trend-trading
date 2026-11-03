# docs

现行规范与契约。一次性的验收 JSON、补丁、历史证据不在本目录，统一归档在
`artifacts/history/docs/`。

## 谁写入

维护者写入仍然有效的规范；每次结构变更同步更新对应契约，不新增平行版本目录。

## 谁读取

`README.md`、`src/` 子包 README、测试与运维人员读取。规范中引用的入口必须在
`src/`、`scripts/` 或 `config/`、`execution/` 里真实存在。

## 什么可以删

规范在替代版本落地且引用清零前不得删除。历史版本移入 `artifacts/history/docs/`，
不留在本目录。
