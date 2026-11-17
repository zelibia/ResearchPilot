# demo_data/ 说明

**这里不放原始数据。**

现有的 40 GB 原始 SAC 数据位于 `/home/wannnlee/seismic/sac_data`，
是既有科研工作区的资产，本仓库只通过配置项只读引用：

```yaml
paths:
  raw_sac_dir: /home/wannnlee/seismic/sac_data
```

## 为什么不做软链接

`data/raw -> seismic/sac_data` 的软链接看起来方便，但只要有一个脚本对
`data/raw/*` 做通配删除，风险就落到 40 GB 原始数据上。用配置项引用，
路径始终是显式的、可审查的。

## 本目录用途

| 子目录 | 用途 | 是否进版本库 |
|---|---|---|
| `manifests/` | 数据集清单、台站表、DOI 清单等小文本 | 是 |
| `samples/` | 极小样例（单日单台，用于测试） | 是 |
| `interim/` | 中间产物 | 否 |
| `processed/` | 处理结果 | 否 |
