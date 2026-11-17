# data-qd-146 · 组织温室气体盘查

Traceable scope 1 and 2 greenhouse gas inventory with dated emission factors。
业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库（含 SQLite）；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
# 遗留全量盘查（行为与 v1 完全一致）
.venv/bin/carbon-inventory --source fixtures/input.json --output run/report.json
# 电力证书修订台账：三代修订 + 旧批重放
.venv/bin/carbon-inventory --registry run/ledger.db \
  --batch fixtures/registry/batch-01-foundation.json --output run/gen1.json
.venv/bin/carbon-inventory --registry run/ledger.db \
  --batch fixtures/registry/batch-02-add-plant-b.json --output run/gen2.json
.venv/bin/carbon-inventory --registry run/ledger.db \
  --batch fixtures/registry/batch-03-correct-grid-factor.json --output run/gen3.json
.venv/bin/carbon-inventory --registry run/ledger.db \
  --batch fixtures/registry/batch-01-foundation.json --output run/gen1-replay.json
cmp run/gen1.json run/gen1-replay.json  # 重放逐字节一致，代数不回退
.venv/bin/python -m unittest discover -s tests -v
```

本地已有 wheel 时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖 Docker、外部账号或持续服务。

## 两种入口

`--source` 与 `--registry` 互斥，`--output` 两种模式都必填。

- 全量模式：`--source <input.json> --output <report.json>`，报告 `schema_version=1`，
  输入 JSON 最多 8MB，拒绝重复键、非有限数值和未定义字段；源字节 SHA256 随结果保留。
- 台账模式：`--registry <ledger.db> --batch <batch.json> --output <report.json>`，
  报告 `schema_version=2`。数据库首次使用为空、`generation=0`；DB、批次、输出三路径互异，
  数据库与输出均拒绝符号链接。

输入 JSON（含批次）最多 8MB，拒绝重复键、非有限数值和未定义字段。
输出是可独立读取的 JSON；成功后原子替换，输入/计算失败退出 2 并保留旧报告。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的 run/。

## 修订台账要点

批次严格包含 `batch_id`、`base_generation`、`changes`；每个变更为
`{kind, id, value}`，kind 为 factor/activity/certificate，`value=null` 删除、
否则为内部 id 一致的完整替换对象。先应用全部变更再校验最终状态，删除不存在对象、
非法最终引用、冲突批次 ID 和过期基代均整批拒绝。scope=2 活动必须为 kWh 并携带
`residual_factor_id`（market 残余口径），`factor_id` 继续负责 location 口径。

批次身份为键排序 JSON（changes 按 `(kind,id)` 排序）的 UTF-8 SHA256，不做分数等价化简。
同 ID 同内容重放优先于基代检查并返回原代报告字节；同 ID 不同内容拒绝；
新批次基代必须等于当前代，接纳成功恰好加一代。

分配把整数 kWh 分到合法（证书, 活动）对，依次最大化覆盖、最小化 market 排放、
最小化相对上代的总绝对变动、最小化分配向量字典序；全程 Fraction 精确运算，
无浮点、无固定权重折叠，增广次数不随电量规模增长。SQLite 单事务提交实体、
整张分配和该代报告；提交后报告写入失败退出 2，同批重放可修复文件且不再接纳。
状态/历史方案与一致性取舍的完整描述见 [数据契约](docs/contract.md) 第 2.6 节。

## 代码组织

src/carbon_inventory/model.py 为全量模式业务模型，inventory.py 为其分组核算；
registry.py 负责台账实体检修、批次身份、SQLite 世代存储与报告构建，
allocation.py 为精确四层最小费用流分配，common.py 提供受限 JSON 及报告发布，
cli.py 衔接两条数据链路。fixtures 为明确自制样例，代码与样例采用 MIT 许可。
本工具不提供气体级 GWP、价值链 Scope 3、抵扣、边界核证、货币因素或官方排放因子，
也不声称合规盘查；样例虚构，单位换算需提前纳入因子定义。
