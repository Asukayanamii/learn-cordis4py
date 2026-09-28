# 06 · Loader：用 YAML 组装插件树

演示 `cordis.yml` 驱动的插件树、条目状态诊断与 patch 叠加层。

```bash
python examples/06_loader/run.py                      # 使用 cordis.yml
python examples/06_loader/run.py --patch              # 叠加 cordis.patch.yml
python examples/06_loader/run.py --dump               # 打印生效后的配置树
```
